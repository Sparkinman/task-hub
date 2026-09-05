#!/bin/bash
# Every test suite, each against a database of its own.
#
# Safe to run at any time, including while Task Hub is syncing real accounts and
# somebody is using it from another device: nothing here touches the live
# database, the live collections, or any connected service.
#
# TASKHUB_DATA_DIR is what guarantees that, and it is set here rather than left
# to whoever runs the suites -- without it they build their world in the real
# database, which happened once and is why it is not optional.
#
# The directory is unique per run and removed afterwards. Reusing one meant a
# second run in the same container found the previous run's world already there
# and failed on a unique constraint -- and because a rebuild wipes /tmp, the
# first run after every rebuild passed, which made the failure look random.
set -u
cd "$(dirname "$0")"

STAMP="$(date +%s)-$$"
# Copied to /app/ rather than /app/tests: the destination already exists,
# so naming it would nest the copy as /app/tests/tests and every suite
# would keep running whatever version was there before.
# Both, and to /app/ rather than /app/tests: the destination already exists,
# so naming it would nest the copy as /app/tests/tests and every suite would
# keep running whatever version was there before. The application is copied
# too, or the suites test the code as it was at the last image build rather
# than the code as it is now -- which is a test run that proves nothing.
docker compose cp app taskhub:/app/ >/dev/null 2>&1
docker compose cp tests taskhub:/app/ >/dev/null 2>&1
# The guides too. test_docs checks that every service in the catalogue has one,
# and without this it reads whatever was baked into the image at the last build
# -- so adding a service and its guide together still failed, pointing at the
# guide that was sitting right there on disk.
docker compose cp docs taskhub:/app/ >/dev/null 2>&1

failed=0
for t in tests/test_*.py; do
  n=$(basename "$t" .py)
  dir="/tmp/suite-$STAMP/$n"
  out=$(docker compose exec -T -e PYTHONPATH=/app -e "TASKHUB_DATA_DIR=$dir" \
        -w /app taskhub python -m "tests.$n" 2>&1)
  if [ $? -eq 0 ]; then
    printf '  \033[32mPASS\033[0m  %-22s %s\n' "$n" "$(echo "$out" | tail -1)"
  else
    failed=$((failed + 1))
    printf '  \033[31mFAIL\033[0m  %-22s\n' "$n"
    echo "$out" | tail -12 | sed 's/^/          /'
  fi
done

docker compose exec -T taskhub rm -rf "/tmp/suite-$STAMP" >/dev/null 2>&1
echo
if [ "$failed" -gt 0 ]; then
  echo "  $failed suite(s) failed."
  exit 1
fi
echo "  All suites passed."
