/*
 * Convo-Ai Zepp OS Mini Program
 * UI component with Start/Stop buttons
 */

import { startRecording, stopRecording } from './recorder.js';

// Simple UI components
export function createStartButton() {
  return {
    type: 'button',
    label: 'Start',
    action: () => startRecording()
  };
}

export function createStopButton() {
  return {
    type: 'button',
    label: 'Stop',
    action: () => stopRecording()
  };
}

export function createStatusView() {
  return {
    type: 'text',
    label: 'Ready',
    update: (text) => {
      // Update label with text
    }
  };
}
