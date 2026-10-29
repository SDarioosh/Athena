// SANDBOX-ONLY COMPILE STUB, written by Athena. NOT Zoom's headers and NOT derived from them.
//
// Declares just the Zoom Meeting SDK for Linux names that meeting-bot/bot/src/main.cpp uses,
// modelled on the calls of the Phase 0 spike's athena_spike.cpp (which compiled and ran
// against the real SDK 7.1.5; removed, in git history at b96332dce842). Purpose: compile-check the bot in a sandbox or CI with
// -DZOOM_SDK_DIR=meeting-bot/bot/sdkstub. Enum values here are placeholders, not Zoom's;
// the binary built against this stub never talks to Zoom (see stub_meetingsdk.cpp).
// Real builds use the owner's unpacked SDK (-DZOOM_SDK_DIR=$HOME/zoom-sdk).
#pragma once

#include <cstdint>
#include <ctime>

#define ATHENA_ZOOM_SDK_STUB 1
#define USING_ZOOM_SDK_NAMESPACE using namespace ZOOMSDK;

namespace ZOOMSDK {

typedef char zchar_t;

enum SDKError {
  SDKERR_SUCCESS = 0,
  SDKERR_NO_IMPL,
  SDKERR_WRONG_USAGE,
  SDKERR_INVALID_PARAMETER,
  SDKERR_UNINITIALIZE = 7,
  SDKERR_NO_PERMISSION = 12,
};

enum SDK_LANGUAGE_ID { LANGUAGE_Unknown = 0, LANGUAGE_English };

struct InitParam {
  const zchar_t* strWebDomain = nullptr;
  const zchar_t* strSupportUrl = nullptr;
  SDK_LANGUAGE_ID emLanguageID = LANGUAGE_Unknown;
  bool enableGenerateDump = false;
  bool enableLogByDefault = false;
};

template <class T>
class IList {
 public:
  virtual ~IList() {}
  virtual int GetCount() = 0;
  virtual T GetItem(int index) = 0;
};

// ---- auth ----
enum AuthResult { AUTHRET_SUCCESS = 0, AUTHRET_KEYORSECRETEMPTY, AUTHRET_JWTTOKENWRONG = 9 };
enum LOGINSTATUS { LOGIN_IDLE = 0 };
enum LoginFailReason { LoginFail_None = 0 };
class IAccountInfo;
struct AuthContext {
  const zchar_t* jwt_token = nullptr;
};
class IAuthServiceEvent {
 public:
  virtual ~IAuthServiceEvent() {}
  virtual void onAuthenticationReturn(AuthResult ret) = 0;
  virtual void onLoginReturnWithReason(LOGINSTATUS, IAccountInfo*, LoginFailReason) = 0;
  virtual void onLogout() = 0;
  virtual void onZoomIdentityExpired() = 0;
  virtual void onZoomAuthIdentityExpired() = 0;
};
class IAuthService {
 public:
  virtual ~IAuthService() {}
  virtual SDKError SetEvent(IAuthServiceEvent* ev) = 0;
  virtual SDKError SDKAuth(AuthContext& ctx) = 0;
};

// ---- settings ----
class IAudioSettingContext {
 public:
  virtual ~IAudioSettingContext() {}
  virtual SDKError EnableAutoJoinAudio(bool enable) = 0;
};
class IVideoSettingContext {
 public:
  virtual ~IVideoSettingContext() {}
  virtual SDKError EnableAutoTurnOffVideoWhenJoinMeeting(bool enable) = 0;
};
class ISettingService {
 public:
  virtual ~ISettingService() {}
  virtual IAudioSettingContext* GetAudioSettings() = 0;
  virtual IVideoSettingContext* GetVideoSettings() = 0;
};

// ---- participants ----
class IUserInfo {
 public:
  virtual ~IUserInfo() {}
  virtual const zchar_t* GetUserName() = 0;
  virtual unsigned int GetUserID() = 0;
};
class IMeetingParticipantsController {
 public:
  virtual ~IMeetingParticipantsController() {}
  virtual IList<unsigned int>* GetParticipantsList() = 0;
  virtual IUserInfo* GetUserByUserID(unsigned int userid) = 0;
  virtual IUserInfo* GetMySelfUser() = 0;
};

// ---- audio ----
class IMeetingAudioController {
 public:
  virtual ~IMeetingAudioController() {}
  virtual SDKError JoinVoip() = 0;
};

// ---- chat ----
enum SDKChatMessageDeleteType { SDK_CHAT_DELETE_BY_NONE = 0 };
struct ChatStatus;
class ISDKFileSender;
class ISDKFileReceiver;
struct SDKFileTransferInfo;
class IChatMsgInfo {
 public:
  virtual ~IChatMsgInfo() {}
  virtual unsigned int GetSenderUserId() = 0;
  virtual const zchar_t* GetSenderDisplayName() = 0;
  virtual const zchar_t* GetContent() = 0;
  virtual time_t GetTimeStamp() = 0;
  virtual bool IsChatToAll() = 0;
};
class IMeetingChatCtrlEvent {
 public:
  virtual ~IMeetingChatCtrlEvent() {}
  virtual void onChatMsgNotification(IChatMsgInfo* chatMsg, const zchar_t* content = nullptr) = 0;
  virtual void onChatStatusChangedNotification(ChatStatus* status) = 0;
  virtual void onChatMsgDeleteNotification(const zchar_t* msgID, SDKChatMessageDeleteType t) = 0;
  virtual void onChatMessageEditNotification(IChatMsgInfo* chatMsg) = 0;
  virtual void onShareMeetingChatStatusChanged(bool isStart) = 0;
  virtual void onFileSendStart(ISDKFileSender* sender) = 0;
  virtual void onFileReceived(ISDKFileReceiver* receiver) = 0;
  virtual void onFileTransferProgress(SDKFileTransferInfo* info) = 0;
};
class IMeetingChatController {
 public:
  virtual ~IMeetingChatController() {}
  virtual SDKError SetEvent(IMeetingChatCtrlEvent* ev) = 0;
};

// ---- recording ----
enum RecordingStatus { Recording_Start = 0 };
enum RequestLocalRecordingStatus {
  RequestLocalRecording_Granted = 0,
  RequestLocalRecording_Denied,
  RequestLocalRecording_Timeout,
};
enum RequestStartCloudRecordingStatus { RequestStartCloudRecording_Granted = 0 };
enum TranscodingStatus { TranscodingStatus_Start = 0 };
class IRequestLocalRecordingPrivilegeHandler;
class IRequestStartCloudRecordingHandler;
class IRequestEnableAndStartSmartRecordingHandler;
class ISmartRecordingEnableActionHandler;
class IMeetingRecordingCtrlEvent {
 public:
  virtual ~IMeetingRecordingCtrlEvent() {}
  virtual void onRecordingStatus(RecordingStatus status) = 0;
  virtual void onCloudRecordingStatus(RecordingStatus status) = 0;
  virtual void onRecordPrivilegeChanged(bool bCanRec) = 0;
  virtual void onLocalRecordingPrivilegeRequestStatus(RequestLocalRecordingStatus status) = 0;
  virtual void onRequestCloudRecordingResponse(RequestStartCloudRecordingStatus status) = 0;
  virtual void onLocalRecordingPrivilegeRequested(IRequestLocalRecordingPrivilegeHandler* h) = 0;
  virtual void onStartCloudRecordingRequested(IRequestStartCloudRecordingHandler* h) = 0;
  virtual void onCloudRecordingStorageFull(time_t gracePeriodDate) = 0;
  virtual void onEnableAndStartSmartRecordingRequested(
      IRequestEnableAndStartSmartRecordingHandler* h) = 0;
  virtual void onSmartRecordingEnableActionCallback(ISmartRecordingEnableActionHandler* h) = 0;
  virtual void onTranscodingStatusChanged(TranscodingStatus status, const zchar_t* path) = 0;
};
class IMeetingRecordingController {
 public:
  virtual ~IMeetingRecordingController() {}
  virtual SDKError SetEvent(IMeetingRecordingCtrlEvent* ev) = 0;
  virtual SDKError CanStartRawRecording() = 0;
  virtual SDKError StartRawRecording() = 0;
  virtual SDKError RequestLocalRecordingPrivilege() = 0;
};

// ---- breakout rooms (meeting_service_components/meeting_breakout_rooms_interface_v2.h) ----
class IMeetingBOController {
 public:
  virtual ~IMeetingBOController() {}
  virtual bool IsBOStarted() = 0;
};

// ---- meeting service ----
enum MeetingStatus {
  MEETING_STATUS_IDLE = 0,
  MEETING_STATUS_CONNECTING,
  MEETING_STATUS_WAITINGFORHOST,
  MEETING_STATUS_INMEETING,
  MEETING_STATUS_DISCONNECTING,
  MEETING_STATUS_RECONNECTING,
  MEETING_STATUS_FAILED,
  MEETING_STATUS_ENDED,
  MEETING_STATUS_IN_WAITING_ROOM = 12,
};
// Placeholder values: main.cpp only compares against the names.
enum MeetingFailCode {
  MEETING_SUCCESS = 0,
  MEETING_FAIL_PASSWORD_ERR = 4,
  MEETING_FAIL_MEETING_NOT_START = 7,
  MEETING_FAIL_MEETING_NOT_EXIST = 8,
  MEETING_FAIL_CANNOT_START_TOKENEXPIRE = 16,
  MEETING_FAIL_AUTHORIZED_USER_NOT_INMEETING = 1143,
};
enum EndMeetingReason {
  EndMeetingReason_None = 0,
  EndMeetingReason_KickByHost,
  EndMeetingReason_EndByHost,
  EndMeetingReason_JBHTimeOut,
};
enum LeaveMeetingCmd { LEAVE_MEETING = 0, END_MEETING };
enum SDKUserType { SDK_UT_NORMALUSER = 0, SDK_UT_WITHOUT_LOGIN };
enum StatisticsWarningType { Statistics_Warning_None = 0 };
enum MeetingComponentType { MeetingComponentType_Def = 0 };
enum ConnectionQuality { Conn_Quality_Unknown = 0 };
struct MeetingParameter;

struct JoinParam4WithoutLogin {
  uint64_t meetingNumber = 0;
  const zchar_t* vanityID = nullptr;
  const zchar_t* userName = nullptr;
  const zchar_t* psw = nullptr;
  const zchar_t* app_privilege_token = nullptr;
  const zchar_t* userZAK = nullptr;
  const zchar_t* customer_key = nullptr;
  const zchar_t* webinarToken = nullptr;
  const zchar_t* onBehalfToken = nullptr;
  bool isVideoOff = false;
  bool isAudioOff = false;
};
struct JoinParam {
  SDKUserType userType = SDK_UT_WITHOUT_LOGIN;
  union Param {
    JoinParam4WithoutLogin withoutloginuserJoin;
    Param() : withoutloginuserJoin() {}
  } param;
};

class IMeetingInfo {
 public:
  virtual ~IMeetingInfo() {}
  virtual const zchar_t* GetMeetingTopic() = 0;
};

class IMeetingServiceEvent {
 public:
  virtual ~IMeetingServiceEvent() {}
  virtual void onMeetingStatusChanged(MeetingStatus status, int iResult = 0) = 0;
  virtual void onMeetingStatisticsWarningNotification(StatisticsWarningType type) = 0;
  virtual void onMeetingParameterNotification(const MeetingParameter* p) = 0;
  virtual void onSuspendParticipantsActivities() = 0;
  virtual void onAICompanionActiveChangeNotice(bool bActive) = 0;
  virtual void onMeetingTopicChanged(const zchar_t* sTopic) = 0;
  virtual void onMeetingFullToWatchLiveStream(const zchar_t* sLiveStreamUrl) = 0;
  virtual void onUserNetworkStatusChanged(MeetingComponentType type, ConnectionQuality level,
                                          unsigned int userId, bool uplink) = 0;
};

class IMeetingService {
 public:
  virtual ~IMeetingService() {}
  virtual SDKError SetEvent(IMeetingServiceEvent* ev) = 0;
  virtual SDKError Join(JoinParam& param) = 0;
  virtual SDKError Leave(LeaveMeetingCmd cmd) = 0;
  virtual MeetingStatus GetMeetingStatus() = 0;
  virtual IMeetingInfo* GetMeetingInfo() = 0;
  virtual IMeetingParticipantsController* GetMeetingParticipantsController() = 0;
  virtual IMeetingRecordingController* GetMeetingRecordingController() = 0;
  virtual IMeetingChatController* GetMeetingChatController() = 0;
  virtual IMeetingAudioController* GetMeetingAudioController() = 0;
  virtual IMeetingBOController* GetMeetingBOController() = 0;
};

// ---- raw audio ----
class AudioRawData {
 public:
  virtual ~AudioRawData() {}
  virtual char* GetBuffer() = 0;
  virtual unsigned int GetBufferLen() = 0;
  virtual unsigned int GetSampleRate() = 0;
  virtual unsigned int GetChannelNum() = 0;
};
class IZoomSDKAudioRawDataDelegate {
 public:
  virtual ~IZoomSDKAudioRawDataDelegate() {}
  virtual void onMixedAudioRawDataReceived(AudioRawData* data) = 0;
  virtual void onOneWayAudioRawDataReceived(AudioRawData* data, uint32_t node_id) = 0;
  virtual void onShareAudioRawDataReceived(AudioRawData* data, uint32_t node_id) = 0;
  virtual void onOneWayInterpreterAudioRawDataReceived(AudioRawData* data,
                                                       const zchar_t* lang) = 0;
};
class IZoomSDKAudioRawDataHelper {
 public:
  virtual ~IZoomSDKAudioRawDataHelper() {}
  virtual SDKError subscribe(IZoomSDKAudioRawDataDelegate* d, bool withInterpreters = false) = 0;
  virtual SDKError unSubscribe() = 0;
};

// ---- free functions ----
extern "C" {
SDKError InitSDK(InitParam& param);
SDKError CleanUPSDK();
SDKError CreateMeetingService(IMeetingService** out);
SDKError DestroyMeetingService(IMeetingService* svc);
SDKError CreateAuthService(IAuthService** out);
SDKError DestroyAuthService(IAuthService* svc);
SDKError CreateSettingService(ISettingService** out);
SDKError DestroySettingService(ISettingService* svc);
IZoomSDKAudioRawDataHelper* GetAudioRawdataHelper();
}

}  // namespace ZOOMSDK
