# shellbox: pip and npm install into its home, the only place that survives the reset
export PIP_BREAK_SYSTEM_PACKAGES=1
export NPM_CONFIG_PREFIX="$HOME/.local"

# The greeting, once per terminal. SHELLBOX_TERMINAL and SHELLBOX_OWNER come from the
# daemon's tmux session; SHELLBOX_GREETED keeps a login shell inside quiet (`sudo -i`
# drops all three and prints nothing). The agent's own rc files may change or drop it.
if [ -n "$SHELLBOX_TERMINAL" ] && [ -z "$SHELLBOX_GREETED" ]; then
    __shellbox_gb() {
        awk -v b="$1" 'BEGIN { v = sprintf("%.1f", b / 1073741824); sub(/\.0$/, "", v); printf "%s", v }'
    }
    __shellbox_greet() {
        local os used size mem_used mem_max quota period cpus owner who notes swatches n
        local version built reset when
        local bold magenta blue green yellow plain
        bold=$(printf '\033[1m') magenta=$(printf '\033[35m') blue=$(printf '\033[34m')
        green=$(printf '\033[32m') yellow=$(printf '\033[33m') plain=$(printf '\033[0m')
        os=$(sed -n 's/^VERSION_ID="\(.*\)"/\1/p' /etc/os-release)
        # its home's size and how full it is, from the daemon's measure (SHELLBOX_HOME_GB);
        # without one, the disk its home is on
        if [ -r /var/lib/shellbox/home ]; then
            read -r used size < /var/lib/shellbox/home
        else
            used=$(df -B1 --output=used ~ | tail -n 1)
            size=$(df -B1 --output=size ~ | tail -n 1)
        fi
        if [ -r /sys/fs/cgroup/memory.current ]; then
            read -r mem_used < /sys/fs/cgroup/memory.current
            read -r mem_max < /sys/fs/cgroup/memory.max
        else
            mem_used=$(awk '/^MemTotal/ { t = $2 } /^MemAvailable/ { a = $2 } END { print (t - a) * 1024 }' /proc/meminfo)
            mem_max=max
        fi
        if [ "$mem_max" = max ]; then
            mem_max=$(awk '/^MemTotal/ { print $2 * 1024 }' /proc/meminfo)
        fi
        # the CPUs the box may use: its cgroup's quota over its period; nproc counts the host's
        quota=max
        [ -r /sys/fs/cgroup/cpu.max ] && read -r quota period < /sys/fs/cgroup/cpu.max
        if [ "$quota" = max ]; then
            cpus=$(nproc)
        else
            cpus=$(awk -v q="$quota" -v p="$period" 'BEGIN { printf "%g", q / p }')
        fi
        [ "$cpus" = 1 ] && cpus="1 CPU" || cpus="$cpus CPUs"
        version=$(sed -n 's/^version //p' /etc/shellbox/version 2>/dev/null)
        built=$(sed -n 's/^built //p' /etc/shellbox/version 2>/dev/null)
        built=$(date -d "$built" '+%-d %b %H:%M' 2>/dev/null)
        reset=$(date -d "$(cat /var/lib/shellbox/reset 2>/dev/null)" '+%-d %b %H:%M' 2>/dev/null)
        when=
        [ -n "$reset" ] && when="last reset $reset, next "
        owner=${SHELLBOX_OWNER:-celine}
        if [ "$owner" = celine ]; then
            who="${magenta}celine${plain}@shellbox, terminal ${bold}${SHELLBOX_TERMINAL}${plain}"
            notes="boot script: ~/boot.sh"
        else
            who="${magenta}${owner}${plain} in celine's shellbox, terminal ${bold}${SHELLBOX_TERMINAL}${plain}"
            notes="her boot script: ~/boot.sh"
        fi
        swatches=
        for n in 0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
            swatches="$swatches$(printf '\033[48;5;%dm  ' "$n")"
        done
        printf '%s%s  .:::.   .:::.  %s   %s\n' "$bold" "$magenta" "$plain" "$who"
        printf '%s%s :::::::.::::::: %s   %s%s, box %s, built %s, %s\n' \
            "$bold" "$magenta" "$plain" "${blue}Debian $os" "$plain" "${version:-unknown}" \
            "${built:-unknown}" "${SHELL##*/}"
        printf '%s%s ::::::::::::::: %s   home %s of %s GB, memory %s of %s GB, %s\n' \
            "$bold" "$magenta" "$plain" "${green}$(__shellbox_gb "$used")${plain}" \
            "$(__shellbox_gb "$size")" "${green}$(__shellbox_gb "$mem_used")${plain}" \
            "$(__shellbox_gb "$mem_max")" "$cpus"
        printf "%s%s  ':::::::::::'  %s   %sreset %s\n" \
            "$bold" "$magenta" "$plain" "$when" "${yellow}${SHELLBOX_RESET_AT}${plain}"
        printf "%s%s    ':::::::'    %s   %s\n" "$bold" "$magenta" "$plain" "$notes"
        printf "%s%s      ':::'      %s   rules: /etc/shellbox/README.md\n" "$bold" "$magenta" "$plain"
        printf "%s%s        '        %s   %s%s\n" "$bold" "$magenta" "$plain" "$swatches" "$plain"
    }
    __shellbox_greet
    unset -f __shellbox_greet __shellbox_gb
    export SHELLBOX_GREETED=1
fi
