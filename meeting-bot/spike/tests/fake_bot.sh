#!/usr/bin/env bash
# Stand-in for the spike bot in tests: prints the bot's STATE lines and burns a little
# CPU. FAKE_LEAVE_BEHIND=1 leaves a detached process listening on a TCP port and a temp
# file, as a badly behaved bot would.
set -euo pipefail
echo "STATE starting"
sleep 1
echo "STATE waiting_permission"
sleep 1
echo "STATE recording"
end=$((SECONDS + ${FAKE_SECONDS:-3}))
while ((SECONDS < end)); do :; done
if [[ "${FAKE_LEAVE_BEHIND:-0}" == "1" ]]; then
  setsid python3 -c 'import socket, time
s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(); time.sleep(60)' \
    > /dev/null 2>&1 < /dev/null &
  mktemp -t athena-spike-test.XXXXXX > /dev/null
  sleep 1
fi
echo "STATE ended duration"
