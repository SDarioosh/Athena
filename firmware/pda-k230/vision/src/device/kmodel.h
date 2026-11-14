// A kmodel on the K230's KPU through the nncase runtime: load, the input tensor, run,
// float outputs. Never terminates the program on an nncase error (no unwrap/expect).
#pragma once

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include <nncase/functional/ai2d/ai2d_builder.h>
#include <nncase/runtime/interpreter.h>
#include <nncase/runtime/runtime_tensor.h>

namespace av::dev {

class KModel {
public:
    bool load(const std::string &path, std::string *err);
    bool loaded() const { return loaded_; }

    // Input 0's shape (N, C, H, W) and the tensor the preprocessing writes into.
    const std::vector<size_t> &input_shape() const { return in_shape_; }
    nncase::runtime::runtime_tensor &input() { return input_; }

    bool run(std::string *err);

    // Output i as floats; valid until the next run(). nullptr if missing or not float32.
    const float *output(size_t i) const;
    size_t output_count() const { return out_ptrs_.size(); }
    const std::vector<size_t> &output_shape(size_t i) const { return out_shapes_[i]; }
    size_t output_elems(size_t i) const;

private:
    nncase::runtime::interpreter interp_;
    std::vector<unsigned char> data_;   // the interpreter refers to it while loaded
    bool loaded_ = false;
    std::vector<size_t> in_shape_;
    nncase::runtime::runtime_tensor input_;
    std::vector<nncase::runtime::runtime_tensor> outputs_;
    std::vector<std::vector<size_t>> out_shapes_;
    std::vector<nncase::runtime::mapped_buffer> out_maps_;
    std::vector<const float *> out_ptrs_;
};

// One camera frame as the preprocessing input: 1 x 3 x H x W uint8 in KPU-shared memory.
class FrameTensor {
public:
    bool init(int width, int height, std::string *err);
    // Copies the planar frame in and writes it back from the cache for the ai2d engine.
    bool load(const uint8_t *data, size_t bytes, std::string *err);
    nncase::runtime::runtime_tensor &tensor() { return tensor_; }
    int width() const { return w_; }
    int height() const { return h_; }

private:
    nncase::runtime::runtime_tensor tensor_;
    int w_ = 0, h_ = 0;
};

// ai2d letterbox: the whole frame scaled to fit the model input, keeping its aspect, the
// rest filled with pad. centred: padding on both sides (yolo style), else right/bottom.
class Letterbox {
public:
    bool init(FrameTensor &in, nncase::runtime::runtime_tensor &out, const int pad[3],
              bool centred, std::string *err);
    bool run(FrameTensor &in, nncase::runtime::runtime_tensor &out, std::string *err);
    // model input pixel = frame pixel * scale + offset
    float scale = 1.0f, off_x = 0.0f, off_y = 0.0f;

private:
    std::unique_ptr<nncase::F::k230::ai2d_builder> builder_;
};

// ai2d crop + resize of a frame region into out (a new schedule each call, as the
// region changes every frame).
bool crop_resize(FrameTensor &in, nncase::runtime::runtime_tensor &out, int x, int y, int w,
                 int h, std::string *err);

}  // namespace av::dev
