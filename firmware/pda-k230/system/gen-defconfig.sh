#!/bin/sh
# gen-defconfig.sh BASE_DEFCONFIG FRAGMENT: print BASE_DEFCONFIG with FRAGMENT applied.
#
# FRAGMENT lines (br2-external/board/defconfig.fragment):
#   KEY=value / # KEY is not set   replace KEY's line in BASE, or append it
#   KEY+="value"                   append value to KEY's string (space separated);
#                                  KEY="value" when BASE has no KEY
#   other comments, blank lines    ignored
# Used by build-image.sh; works with any POSIX awk.
set -eu

base="${1:?usage: gen-defconfig.sh BASE_DEFCONFIG FRAGMENT}"
frag="${2:?usage: gen-defconfig.sh BASE_DEFCONFIG FRAGMENT}"
[ -r "$base" ] || { echo "gen-defconfig.sh: can't read $base" >&2; exit 1; }
[ -r "$frag" ] || { echo "gen-defconfig.sh: can't read $frag" >&2; exit 1; }

awk -v fragfile="$frag" '
function keyof(line,    m) {
	if (line ~ /^BR2_[A-Za-z0-9_]+(\+)?=/) {
		m = line
		sub(/\+?=.*/, "", m)
		return m
	}
	if (line ~ /^# BR2_[A-Za-z0-9_]+ is not set$/) {
		m = line
		sub(/^# /, "", m)
		sub(/ is not set$/, "", m)
		return m
	}
	return ""
}
function unquote(v) {
	sub(/^"/, "", v)
	sub(/"$/, "", v)
	return v
}
BEGIN {
	n = 0
}
{
	lines[++n] = $0
	k = keyof($0)
	if (k != "") at[k] = n
}
END {
	extra = 0
	while ((getline f < fragfile) > 0) {
		k = keyof(f)
		if (k == "") continue
		if (f ~ /^BR2_[A-Za-z0-9_]+\+=/) {
			add = f
			sub(/^[^=]*\+=/, "", add)
			add = unquote(add)
			if (k in at && lines[at[k]] ~ /^BR2_[A-Za-z0-9_]+="/) {
				old = lines[at[k]]
				sub(/^[^=]*=/, "", old)
				old = unquote(old)
				lines[at[k]] = k "=\"" (old == "" ? add : old " " add) "\""
			} else if (k in at) {
				lines[at[k]] = k "=\"" add "\""
			} else {
				appended[++extra] = k "=\"" add "\""
				at[k] = -1
			}
		} else if (k in at && at[k] > 0) {
			lines[at[k]] = f
		} else {
			appended[++extra] = f
			at[k] = -1
		}
	}
	for (i = 1; i <= n; i++) print lines[i]
	if (extra > 0) {
		print ""
		print "# Athena PDA (firmware/pda-k230/system/br2-external/board/defconfig.fragment)"
		for (i = 1; i <= extra; i++) print appended[i]
	}
}
' "$base"
