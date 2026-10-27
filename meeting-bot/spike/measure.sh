#!/usr/bin/env bash
# Measure one spike bot run (Phase 0 of docs/zoom-meeting-bot-plan.md): CPU, peak RSS,
# time from start to "in meeting" (the first `STATE waiting_permission` or `STATE recording`
# line) and to the first `STATE recording`, and what is left behind after it
# exits (processes, listening sockets, temp files). Throwaway dev tool, not in CI.
#
# Launch the bot through it (preferred; timing starts at launch):
#   ./measure.sh -l cpp-30min -- ./run.sh -- --meeting-id 812... --passcode ... --minutes 30
# Or attach to a running bot, with the file its stdout goes to:
#   ./measure.sh -l py-30min -p <pid> -L bot.log
#
# Options: -o results file (default results/measurements.md), -l label, -i pidstat
# interval in seconds (default 5). Writes <results dir>/<label>-<time>/ (bot.log, pidstat.txt,
# samples.tsv, summary.md) and appends one row to the results file in the format of the
# measurement table in docs/meeting-bot-spike.md. Exit status 1 if anything was left
# behind. CPU: 100 % = one core.
set -euo pipefail

usage() {
  sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
results="$here/results/measurements.md"
label="run"
interval=5
attach_pid=""
attach_log=""
while getopts ":o:l:i:p:L:h" opt; do
  case "$opt" in
    o) results="$OPTARG" ;;
    l) label="$OPTARG" ;;
    i) interval="$OPTARG" ;;
    p) attach_pid="$OPTARG" ;;
    L) attach_log="$OPTARG" ;;
    *) usage ;;
  esac
done
shift $((OPTIND - 1))
if [[ "${1:-}" == "--" ]]; then shift; fi
if [[ -z "$attach_pid" && $# -eq 0 ]]; then usage; fi
if [[ -n "$attach_pid" && -z "$attach_log" ]]; then
  echo "measure.sh: -p needs -L <file the bot's stdout goes to>" >&2
  exit 2
fi
[[ "$interval" =~ ^[0-9]+$ && "$interval" -ge 1 ]] || { echo "bad -i" >&2; exit 2; }

clk_tck="$(getconf CLK_TCK)"
run_dir="$(dirname "$results")/${label}-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$run_dir"
samples="$run_dir/samples.tsv"
temp_dirs=(/tmp /dev/shm)
if [[ -n "${XDG_RUNTIME_DIR:-}" && -d "$XDG_RUNTIME_DIR" ]]; then temp_dirs+=("$XDG_RUNTIME_DIR"); fi

list_temp() {
  local d
  for d in "${temp_dirs[@]}"; do
    find "$d" -mindepth 1 -maxdepth 1 -printf '%p\n' 2>/dev/null || true
  done | sort
}

list_temp > "$run_dir/temp.before"
ps -e -o pid= | tr -d ' ' | sort > "$run_dir/pids.before"

# --- start or attach ---------------------------------------------------------------------
if [[ -n "$attach_pid" ]]; then
  pid="$attach_pid"
  log="$attach_log"
  kill -0 "$pid" 2>/dev/null || { echo "measure.sh: no process $pid" >&2; exit 2; }
  # process start time = boot time + starttime ticks (field 22 of /proc/<pid>/stat)
  btime="$(awk '/^btime/ {print $2}' /proc/stat)"
  start_ticks="$(sed 's/^.*) //' "/proc/$pid/stat" | awk '{print $20}')"
  start="$(awk -v b="$btime" -v t="$start_ticks" -v c="$clk_tck" 'BEGIN {printf "%.2f", b + t / c}')"
  sid="$(ps -o sid= -p "$pid" | tr -d ' ')"
  own_session=0
else
  log="$run_dir/bot.log"
  start="$(date +%s.%N)"
  # own session, so every helper the bot starts can be found afterwards
  setsid "$@" > "$log" 2>&1 &
  pid=$!
  sid="$pid"
  own_session=1
fi
echo "measure.sh: pid $pid, log $log, results in $run_dir"

pidstat_pid=""
if command -v pidstat > /dev/null; then
  pidstat -u -r -h -p "$pid" "$interval" > "$run_dir/pidstat.txt" 2>&1 &
  pidstat_pid=$!
else
  echo "pidstat not found (apt install sysstat); using /proc samples only" > "$run_dir/pidstat.txt"
fi

# processes that belong to this bot: its session (launch mode) or its descendants
bot_pids() {
  if [[ "$own_session" == 1 ]]; then
    pgrep -s "$sid" || true
  else
    local todo=("$pid") p kids
    while ((${#todo[@]})); do
      p="${todo[0]}"
      todo=("${todo[@]:1}")
      [[ -d "/proc/$p" ]] && echo "$p"
      kids="$(pgrep -P "$p" || true)"
      if [[ -n "$kids" ]]; then mapfile -t -O "${#todo[@]}" todo <<< "$kids"; fi
    done
  fi
}

cpu_ticks() {  # utime + stime of the given pids, in ticks
  local p total=0 t
  for p in "$@"; do
    t="$(sed 's/^.*) //' "/proc/$p/stat" 2>/dev/null | awk '{print $12 + $13}')" || t=0
    total=$((total + ${t:-0}))
  done
  echo "$total"
}

alive() {  # running and not a zombie waiting for its parent
  kill -0 "$1" 2>/dev/null && [[ "$(sed 's/^.*) //' "/proc/$1/stat" 2>/dev/null | cut -c1)" != Z ]]
}

# --- sample until the bot exits ----------------------------------------------------------
printf 'epoch\tcpu_pct\trss_kb\n' > "$samples"
declare -A seen=()
declare -A opened_temp=()
peak_hwm_kb=0
t_meeting=""
t_recording=""
last_ticks=0
last_t="$(date +%s.%N)"
tick=0
while alive "$pid"; do
  mapfile -t pids < <(bot_pids)
  rss_total=0
  for p in "${pids[@]}"; do
    seen[$p]=1
    # temp files the bot holds open (to tell its leftovers from other programs' files)
    while read -r f; do [[ -n "$f" ]] && opened_temp[$f]=1; done < <(
      find "/proc/$p/fd" \( -lname '/tmp/*' -o -lname '/dev/shm/*' \) -printf '%l\n' 2>/dev/null || true)
    hwm="$(awk '/^VmHWM:/ {print $2}' "/proc/$p/status" 2>/dev/null || true)"
    rss="$(awk '/^VmRSS:/ {print $2}' "/proc/$p/status" 2>/dev/null || true)"
    if [[ -n "$hwm" ]] && ((hwm > peak_hwm_kb)); then peak_hwm_kb="$hwm"; fi
    rss_total=$((rss_total + ${rss:-0}))
  done
  if [[ -z "$t_meeting" ]] && grep -qE '^STATE (waiting_permission|recording)' "$log" 2>/dev/null; then
    t_meeting="$(awk -v n="$(date +%s.%N)" -v s="$start" 'BEGIN {printf "%.1f", n - s}')"
    echo "measure.sh: in the meeting after ${t_meeting} s"
  fi
  if [[ -z "$t_recording" ]] && grep -q '^STATE recording' "$log" 2>/dev/null; then
    t_recording="$(awk -v n="$(date +%s.%N)" -v s="$start" 'BEGIN {printf "%.1f", n - s}')"
    echo "measure.sh: first STATE recording after ${t_recording} s"
  fi
  if ((tick % interval == 0)); then
    now="$(date +%s.%N)"
    ticks="$(cpu_ticks "${pids[@]}")"
    if ((tick > 0)); then
      awk -v n="$now" -v l="$last_t" -v d=$((ticks - last_ticks)) -v c="$clk_tck" -v r="$rss_total" \
        'BEGIN {dt = n - l; if (dt <= 0) dt = 1; printf "%d\t%.1f\t%d\n", n, d / c / dt * 100, r}' \
        >> "$samples"
    fi
    last_ticks="$ticks"
    last_t="$now"
  fi
  tick=$((tick + 1))
  sleep 1
done
end="$(date +%s.%N)"
exit_status=0
if [[ "$own_session" == 1 ]]; then wait "$pid" || exit_status=$?; fi
if [[ -n "$pidstat_pid" ]]; then
  kill "$pidstat_pid" 2>/dev/null || true
  wait "$pidstat_pid" 2>/dev/null || true
fi
sleep 2  # give helpers a moment to exit on their own before counting leftovers

# --- summarize ---------------------------------------------------------------------------
# CPU from pidstat (%CPU column of the bot's main process) when present, else /proc samples
# (whole bot: all its processes).
cpu_src="proc"
read -r cpu_avg cpu_peak < <(awk -F'\t' 'NR > 1 {s += $2; n++; if ($2 > m) m = $2}
  END {if (n) printf "%.1f %.1f\n", s / n, m; else print "n/a n/a"}' "$samples")
if [[ -n "$pidstat_pid" ]] && grep -q '%CPU' "$run_dir/pidstat.txt"; then
  read -r p_avg p_peak < <(awk '
    /^#/ { for (i = 2; i <= NF; i++) if ($i == "%CPU") col = i - 1; next }
    col && NF > col && $1 ~ /^[0-9]+$/ { v = $col + 0; s += v; n++; if (v > m) m = v }
    END { if (n) printf "%.1f %.1f\n", s / n, m; else print "n/a n/a" }' "$run_dir/pidstat.txt")
  if [[ "$p_avg" != "n/a" ]]; then cpu_avg="$p_avg"; cpu_peak="$p_peak"; cpu_src="pidstat"; fi
fi
duration_min="$(awk -v e="$end" -v s="$start" 'BEGIN {printf "%.1f", (e - s) / 60}')"
peak_mb="$(awk -v k="$peak_hwm_kb" 'BEGIN {printf "%.0f", k / 1024}')"
last_state="$(grep '^STATE ' "$log" 2>/dev/null | tail -n 1 | cut -d' ' -f2- || true)"

# leftovers: bot processes still alive, new orphaned processes, sockets, temp files
leftover_procs=()
for p in "${!seen[@]}"; do
  if alive "$p"; then leftover_procs+=("$p"); fi
done
if [[ "$own_session" == 1 ]]; then
  while read -r p; do [[ -n "$p" ]] && leftover_procs+=("$p"); done < <(pgrep -s "$sid" || true)
fi
ps -e -o pid= | tr -d ' ' | sort > "$run_dir/pids.after"
while read -r p; do
  [[ -z "$p" || "$p" == "$$" ]] && continue
  alive "$p" || continue  # gone, or a zombie waiting for init to reap it: not a leftover
  ppid="$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ' || true)"
  owner="$(ps -o uid= -p "$p" 2>/dev/null | tr -d ' ' || true)"
  if [[ "$ppid" == 1 && "$owner" == "$(id -u)" ]]; then leftover_procs+=("$p"); fi
done < <(comm -13 "$run_dir/pids.before" "$run_dir/pids.after")
mapfile -t leftover_procs < <(printf '%s\n' "${leftover_procs[@]}" | sort -un | sed '/^$/d')

# listening TCP / bound UDP sockets of the given pids, from /proc (when ss is missing)
proc_sockets() {
  local p inodes
  for p in "$@"; do
    inodes="$(find "/proc/$p/fd" -lname 'socket:*' -printf '%l\n' 2>/dev/null \
      | sed 's/[^0-9]//g' | paste -sd' ' || true)"
    [[ -z "$inodes" ]] && continue
    for f in /proc/net/tcp /proc/net/tcp6 /proc/net/udp /proc/net/udp6; do
      [[ -r "$f" ]] || continue
      awk -v ino="$inodes" -v p="$p" -v f="${f##*/}" '
        BEGIN { n = split(ino, a, " "); for (i = 1; i <= n; i++) want[a[i]] = 1 }
        NR > 1 && ($10 in want) && (f ~ /udp/ || $4 == "0A") {
          print f " " $2 " pid=" p }' "$f"
    done
  done
}

sockets=""
if ((${#leftover_procs[@]})); then
  if command -v ss > /dev/null; then
    pat="$(printf 'pid=%s,|' "${leftover_procs[@]}")"
    sockets="$(ss -tulnpH 2>/dev/null | grep -E "${pat%|}" || true)"
  else
    sockets="$(proc_sockets "${leftover_procs[@]}")"
  fi
fi
list_temp > "$run_dir/temp.after"
new_temp="$(comm -13 "$run_dir/temp.before" "$run_dir/temp.after" | grep -v "^$run_dir" || true)"
# Other programs create temp files too. Count a new entry as the bot's when the bot had a
# file under it open, or its name looks like the bot's (zoom, pulse, athena, the command).
bot_name="$(basename "${1:-athena}")"
bot_temp=""
other_temp=""
while read -r t; do
  [[ -z "$t" ]] && continue
  mine=0
  for f in "${!opened_temp[@]}"; do [[ "$f" == "$t" || "$f" == "$t"/* ]] && mine=1; done
  shopt -s nocasematch
  if [[ "$t" =~ zoom|pulse|athena || "$t" == *"$bot_name"* ]]; then mine=1; fi
  shopt -u nocasematch
  if ((mine)); then bot_temp+="$t"$'\n'; else other_temp+="$t"$'\n'; fi
done <<< "$new_temp"
bot_temp="${bot_temp%$'\n'}"
other_temp="${other_temp%$'\n'}"
sdk_logs="none"
if [[ -d "$HOME/.zoomsdk" ]]; then sdk_logs="$(du -sh "$HOME/.zoomsdk" | cut -f1) in ~/.zoomsdk"; fi

left=()
((${#leftover_procs[@]})) && left+=("${#leftover_procs[@]} process(es)")
[[ -n "$sockets" ]] && left+=("listening socket(s)")
[[ -n "$bot_temp" ]] && left+=("$(wc -l <<< "$bot_temp") temp file(s)")
leftovers="none"
if ((${#left[@]})); then leftovers="$(printf '%s, ' "${left[@]}")"; leftovers="${leftovers%, }"; fi

{
  echo "# Spike measurement: $label"
  echo
  echo "- Command: ${*:-attached to pid $pid}"
  echo "- Exit status: $exit_status; last state: ${last_state:-none}"
  echo "- Duration: $duration_min min"
  echo "- CPU avg / peak: $cpu_avg % / $cpu_peak % (source: $cpu_src; 100 % = one core)"
  echo "- Peak RSS: $peak_mb MB (max VmHWM over the bot's processes)"
  echo "- Start to in meeting (first STATE waiting_permission or recording): ${t_meeting:-never} s"
  echo "- Start to first STATE recording (includes the host allowing it): ${t_recording:-never} s"
  echo "- Processes seen: ${!seen[*]}"
  echo "- Left behind: $leftovers"
  if ((${#leftover_procs[@]})); then
    echo
    echo '```'
    ps -o pid,ppid,etimes,cmd -p "$(IFS=,; echo "${leftover_procs[*]}")" || true
    echo '```'
  fi
  fence='```'
  if [[ -n "$sockets" ]]; then printf '\nListening sockets:\n\n%s\n%s\n%s\n' "$fence" "$sockets" "$fence"; fi
  if [[ -n "$bot_temp" ]]; then printf '\nTemp files left by the bot:\n\n%s\n%s\n%s\n' "$fence" "$bot_temp" "$fence"; fi
  if [[ -n "$other_temp" ]]; then
    printf '\nOther new temp files during the run (likely other programs; check):\n\n%s\n%s\n%s\n' \
      "$fence" "$other_temp" "$fence"
  fi
  echo "- Zoom SDK logs (persist between runs): $sdk_logs"
} > "$run_dir/summary.md"

if [[ ! -s "$results" ]]; then
  {
    echo "| Run | Duration (min) | CPU avg % | CPU peak % | Peak RSS MB | Start → in meeting s | Left behind |"
    echo "| --- | --- | --- | --- | --- | --- | --- |"
  } > "$results"
fi
echo "| $label | $duration_min | $cpu_avg | $cpu_peak | $peak_mb | ${t_meeting:-never} | $leftovers |" >> "$results"

cat "$run_dir/summary.md"
echo
echo "Row appended to $results"
[[ "$leftovers" == "none" ]]
