/*
 * Options for sending an audio envelope from the watch to the Side Service.
 * zml treats any non-Buffer object as JSON, including an ArrayBuffer, so without
 * contentType 'bin' it sends {"jsonrpc":"hmrpcv1"} and no audio.
 */
export const UPLOAD_OPTIONS = { contentType: 'bin', dataType: 'json' }
