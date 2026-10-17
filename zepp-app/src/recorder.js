/*
 * Convo-Ai Zepp OS Mini Program
 * Minimal feasibility test app for T-Rex 3 Pro
 * 
 * Features:
 * - Start/Stop recording
 * - 30-second Opus chunks
 * - Logs chunk count
 */

import * as device from '@zeppos/device';
import { Recorder } from '@zos/media';

let recorder = null;
let isRecording = false;
let chunkCount = 0;

// App name and icon (simple text placeholder)
const appName = 'Convo-Ai';

// Initialize app
export async function init() {
  console.log('Convo-Ai app initialized');
  console.log('Press Start to begin recording');
}

// Start recording
export async function startRecording() {
  if (isRecording) {
    console.log('Already recording');
    return;
  }

  console.log('Starting recording...');
  
  // Create recorder
  recorder = Recorder.create('RECORDER');
  
  // Set format to Opus
  await recorder.setFormat(Recorder.codec.OPUS, {
    target_file: 'data://temp/recording.opus'
  });
  
  // Start recording
  await recorder.start();
  
  isRecording = true;
  chunkCount = 0;
  
  console.log('Recording started. Chunks: 0');
  
  // Update UI (simple log for now)
  printRecordingState();
}

// Stop recording
export async function stopRecording() {
  if (!isRecording) {
    console.log('Not recording');
    return;
  }

  console.log('Stopping recording...');
  
  // Stop recorder
  await recorder.stop();
  
  isRecording = false;
  chunkCount++;
  
  console.log(`Recording stopped. Total chunks: ${chunkCount}`);
  
  // Update UI
  printRecordingState();
}

// Print recording state to console
function printRecordingState() {
  const state = isRecording ? 'RECORDING' : 'STOPPED';
  console.log(`State: ${state}, Chunks: ${chunkCount}`);
}

// Event handlers
export function onAppEvent(event) {
  switch (event.type) {
    case 'start':
      startRecording();
      break;
    case 'stop':
      stopRecording();
      break;
    default:
      console.log('Unknown event:', event.type);
  }
}
