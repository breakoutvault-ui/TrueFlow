#!/bin/bash
# ==============================================================
#  tfget - pull a file from GitHub and PROVE it is the right one.
#
#  GitHub caches raw files for 5 minutes at the edge, and version
#  numbers in the URL do not reliably beat it. This waits the cache
#  out instead of you re-running curl and wondering.
#
#  Install once:
#    cd /root/trueflow && chmod +x tfget.sh
#
#  Use:
#    ./tfget.sh study_engine.py 12858
#    ./tfget.sh Index.html.html 1218580
#
#  Second argument is the expected byte count. Leave it off and it
#  just pulls whatever is there and tells you the size.
# ==============================================================

F="$1"
WANT="$2"
URL="https://raw.githubusercontent.com/breakoutvault-ui/TrueFlow/main/$F"
TRIES=12          # 12 x 30s = 6 minutes, longer than the 5 minute cache

if [ -z "$F" ]; then echo "usage: ./tfget.sh <filename> [expected-bytes]"; exit 1; fi

for i in $(seq 1 $TRIES); do
  curl -s -H "Cache-Control: no-cache" -o "/tmp/tfget.$$" "$URL?t=$(date +%s)$RANDOM"
  GOT=$(wc -c < "/tmp/tfget.$$" | tr -d ' ')

  if [ -z "$WANT" ]; then
    mv "/tmp/tfget.$$" "$F"; echo "pulled $F  ($GOT bytes)"; exit 0
  fi

  if [ "$GOT" = "$WANT" ]; then
    mv "/tmp/tfget.$$" "$F"
    echo "OK  $F is $GOT bytes, exactly as expected."
    exit 0
  fi

  AGE=$(curl -sI "$URL" | grep -i '^source-age' | tr -d '\r' | awk '{print $2}')
  echo "try $i/$TRIES: got $GOT bytes, want $WANT (edge cache age ${AGE:-?}s) - waiting 30s"
  sleep 30
done

rm -f "/tmp/tfget.$$"
echo ""
echo "FAILED after $TRIES tries. The file on GitHub is not $WANT bytes."
echo "That means the upload did not commit - check the file on github.com."
exit 1
