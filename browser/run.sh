#!/bin/sh
# Keeps browserd running: starts it again 1 s after it exits, and kills it when its
# /health has not answered for 10 s (a hung browserd answers nothing). browserd's own
# start routine then kills what is left of Xvnc and Brave and starts them again.
while true; do
    python3 /usr/local/bin/browserd &
    pid=$!
    answered=$(date +%s)
    while kill -0 "$pid" 2>/dev/null; do
        sleep 1
        if curl -fs -m 1 -o /dev/null http://127.0.0.1:${BROWSER_PORT:-8771}/health; then
            answered=$(date +%s)
        elif [ $(( $(date +%s) - answered )) -ge 10 ]; then
            echo "run.sh: browserd did not answer for 10 s, killing it"
            kill -9 "$pid"
        fi
    done
    wait "$pid"
    sleep 1
done
