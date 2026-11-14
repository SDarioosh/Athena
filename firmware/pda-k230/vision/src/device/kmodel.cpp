#include "kmodel.h"

#include <algorithm>
#include <cstring>
#include <fstream>
#include <iterator>

namespace av::dev {

using namespace nncase;
using namespace nncase::runtime;

namespace {

template <class R> std::string why(R &r) { return r.unwrap_err().message(); }

std::vector<size_t> to_vec(const dims_t &d) { return std::vector<size_t>(d.begin(), d.end()); }

}  // namespace

bool KModel::load(const std::string &path, std::string *err) {
    std::ifstream f(path, std::ios::binary);
    if (!f) {
        if (err) *err = "can't open " + path;
        return false;
    }
    data_.assign(std::istreambuf_iterator<char>(f), std::istreambuf_iterator<char>());
    if (data_.empty()) {
        if (err) *err = path + " is empty";
        return false;
    }
    auto span = gsl::span<const gsl::byte>(reinterpret_cast<const gsl::byte *>(data_.data()),
                                           data_.size());
    auto r = interp_.load_model(span, false);
    if (!r.is_ok()) {
        if (err) *err = path + ": " + why(r);
        return false;
    }
    if (interp_.inputs_size() < 1) {
        if (err) *err = path + ": no inputs";
        return false;
    }
    for (size_t i = 0; i < interp_.inputs_size(); i++) {
        auto desc = interp_.input_desc(i);
        auto shape = interp_.input_shape(i);
        auto t = hrt::create(desc.datatype, shape, hrt::pool_shared);
        if (!t.is_ok()) {
            if (err) *err = path + ": input tensor: " + why(t);
            return false;
        }
        auto set = interp_.input_tensor(i, t.unwrap());
        if (!set.is_ok()) {
            if (err) *err = path + ": set input: " + why(set);
            return false;
        }
        if (i == 0) {
            auto again = interp_.input_tensor(0);
            if (!again.is_ok()) {
                if (err) *err = path + ": get input: " + why(again);
                return false;
            }
            input_ = again.unwrap();
            in_shape_ = to_vec(shape);
        }
    }
    outputs_.clear();
    out_shapes_.clear();
    for (size_t i = 0; i < interp_.outputs_size(); i++) {
        auto desc = interp_.output_desc(i);
        auto shape = interp_.output_shape(i);
        auto t = hrt::create(desc.datatype, shape, hrt::pool_shared);
        if (!t.is_ok()) {
            if (err) *err = path + ": output tensor: " + why(t);
            return false;
        }
        auto set = interp_.output_tensor(i, t.unwrap());
        if (!set.is_ok()) {
            if (err) *err = path + ": set output: " + why(set);
            return false;
        }
        outputs_.push_back(t.unwrap());
        out_shapes_.push_back(to_vec(shape));
        if (desc.datatype != dt_float32) {
            if (err) *err = path + ": output " + std::to_string(i) + " is not float32";
            return false;
        }
    }
    out_ptrs_.assign(outputs_.size(), nullptr);
    loaded_ = true;
    return true;
}

bool KModel::run(std::string *err) {
    out_maps_.clear();
    for (auto &p : out_ptrs_) p = nullptr;
    auto r = interp_.run();
    if (!r.is_ok()) {
        if (err) *err = "kpu run: " + why(r);
        return false;
    }
    out_maps_.resize(outputs_.size());
    for (size_t i = 0; i < outputs_.size(); i++) {
        auto out = interp_.output_tensor(i);
        if (!out.is_ok()) {
            if (err) *err = "output: " + why(out);
            return false;
        }
        runtime_tensor t = out.unwrap();
        auto s = hrt::sync(t, sync_op_t::sync_invalidate, true);
        (void)s;  // a cache invalidate that isn't needed fails harmlessly
        auto m = hrt::map(t, map_access_t::map_read);
        if (!m.is_ok()) {
            if (err) *err = "map output: " + why(m);
            return false;
        }
        out_maps_[i] = std::move(m.unwrap());
        // the readers index up to output_elems(i) floats: never past the mapped buffer
        if (out_maps_[i].buffer().size_bytes() < output_elems(i) * sizeof(float)) {
            if (err) *err = "output " + std::to_string(i) + " smaller than its shape";
            return false;
        }
        out_ptrs_[i] = reinterpret_cast<const float *>(out_maps_[i].buffer().data());
    }
    return true;
}

const float *KModel::output(size_t i) const { return i < out_ptrs_.size() ? out_ptrs_[i] : nullptr; }

size_t KModel::output_elems(size_t i) const {
    if (i >= out_shapes_.size()) return 0;
    size_t n = 1;
    for (size_t d : out_shapes_[i]) n *= d;
    return n;
}

}  // namespace av::dev

namespace av::dev {

using namespace nncase;
using namespace nncase::runtime;
using namespace nncase::runtime::k230;
using nncase::F::k230::ai2d_builder;

bool FrameTensor::init(int width, int height, std::string *err) {
    dims_t shape{1, 3, static_cast<size_t>(height), static_cast<size_t>(width)};
    auto t = hrt::create(dt_uint8, shape, hrt::pool_shared);
    if (!t.is_ok()) {
        if (err) *err = "frame tensor: " + t.unwrap_err().message();
        return false;
    }
    tensor_ = t.unwrap();
    w_ = width;
    h_ = height;
    return true;
}

bool FrameTensor::load(const uint8_t *data, size_t bytes, std::string *err) {
    size_t need = static_cast<size_t>(w_) * static_cast<size_t>(h_) * 3;
    if (!data || bytes < need) {
        if (err) *err = "short frame";
        return false;
    }
    {
        auto m = hrt::map(tensor_, map_access_t::map_write);
        if (!m.is_ok()) {
            if (err) *err = "map frame tensor: " + m.unwrap_err().message();
            return false;
        }
        auto buf = m.unwrap().buffer();
        if (buf.size() < need) {
            if (err) *err = "frame tensor too small";
            return false;
        }
        std::memcpy(buf.data(), data, need);
    }  // unmapped here
    auto s = hrt::sync(tensor_, sync_op_t::sync_write_back, true);
    if (!s.is_ok()) {
        if (err) *err = "sync frame tensor: " + s.unwrap_err().message();
        return false;
    }
    return true;
}

namespace {

ai2d_resize_param_t bilinear() {
    ai2d_resize_param_t r;
    r.resize_flag = true;
    r.interp_method = ai2d_interp_method::tf_bilinear;
    r.interp_mode = ai2d_interp_mode::half_pixel;
    return r;
}

}  // namespace

bool Letterbox::init(FrameTensor &in, runtime_tensor &out, const int pad[3], bool centred,
                     std::string *err) {
    auto ish = in.tensor().shape();
    auto osh = out.shape();
    if (ish.size() != 4 || osh.size() != 4) {
        if (err) *err = "letterbox: expected NCHW tensors";
        return false;
    }
    float iw = static_cast<float>(ish[3]), ih = static_cast<float>(ish[2]);
    float ow = static_cast<float>(osh[3]), oh = static_cast<float>(osh[2]);
    scale = std::min(ow / iw, oh / ih);
    float nw = iw * scale, nh = ih * scale;
    int pw = static_cast<int>(ow - nw + 0.5f), ph = static_cast<int>(oh - nh + 0.5f);
    int left = centred ? pw / 2 : 0, top = centred ? ph / 2 : 0;
    off_x = static_cast<float>(left);
    off_y = static_cast<float>(top);

    ai2d_datatype_t dt{ai2d_format::NCHW_FMT, ai2d_format::NCHW_FMT, in.tensor().datatype(),
                       out.datatype()};
    ai2d_crop_param_t crop;
    ai2d_shift_param_t shift;
    ai2d_pad_param_t padp;
    padp.pad_flag = true;
    padp.paddings = {{0, 0}, {0, 0}, {top, ph - top}, {left, pw - left}};
    padp.pad_mode = ai2d_pad_mode::constant;
    padp.pad_val = {pad[0], pad[1], pad[2]};
    ai2d_resize_param_t resize = bilinear();
    ai2d_affine_param_t affine;
    dims_t in_shape(ish.begin(), ish.end()), out_shape(osh.begin(), osh.end());
    builder_.reset(new ai2d_builder(in_shape, out_shape, dt, crop, shift, padp, resize, affine));
    auto r = builder_->build_schedule();
    if (!r.is_ok()) {
        if (err) *err = "letterbox schedule: " + r.unwrap_err().message();
        builder_.reset();
        return false;
    }
    return true;
}

bool Letterbox::run(FrameTensor &in, runtime_tensor &out, std::string *err) {
    if (!builder_) {
        if (err) *err = "letterbox not set up";
        return false;
    }
    auto r = builder_->invoke(in.tensor(), out);
    if (!r.is_ok()) {
        if (err) *err = "letterbox: " + r.unwrap_err().message();
        return false;
    }
    return true;
}

bool crop_resize(FrameTensor &in, runtime_tensor &out, int x, int y, int w, int h,
                 std::string *err) {
    if (w <= 1 || h <= 1) {
        if (err) *err = "crop too small";
        return false;
    }
    auto ish = in.tensor().shape();
    auto osh = out.shape();
    ai2d_datatype_t dt{ai2d_format::NCHW_FMT, ai2d_format::NCHW_FMT, in.tensor().datatype(),
                       out.datatype()};
    ai2d_crop_param_t crop;
    crop.crop_flag = true;
    crop.start_x = x;
    crop.start_y = y;
    crop.width = w;
    crop.height = h;
    ai2d_shift_param_t shift;
    ai2d_pad_param_t padp;
    padp.paddings = {{0, 0}, {0, 0}, {0, 0}, {0, 0}};
    padp.pad_val = {0, 0, 0};
    ai2d_resize_param_t resize = bilinear();
    ai2d_affine_param_t affine;
    dims_t in_shape(ish.begin(), ish.end()), out_shape(osh.begin(), osh.end());
    ai2d_builder b(in_shape, out_shape, dt, crop, shift, padp, resize, affine);
    auto s = b.build_schedule();
    if (!s.is_ok()) {
        if (err) *err = "crop schedule: " + s.unwrap_err().message();
        return false;
    }
    auto r = b.invoke(in.tensor(), out);
    if (!r.is_ok()) {
        if (err) *err = "crop: " + r.unwrap_err().message();
        return false;
    }
    return true;
}

}  // namespace av::dev
