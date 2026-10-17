/*
 * Convo-Ai Zepp OS Mini Program
 * Main entry point
 */

import { init } from './recorder.js';

// Initialize app when loaded
init();

// Handle events from UI buttons
window.convoAi = {
  startRecording: () => {
    startRecording();
  },
  stopRecording: () => {
    stopRecording();
  }
};

function startRecording() {
  console.log('Starting recording...');
  Recorder.create('RECORDER').start();
}

function stopRecording() {
  console.log('Stopping recording...');
  Recorder.create('RECORDER').stop();
}
