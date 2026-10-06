# Redis Sentinel verification (throwaway cluster)

Not part of the main app stack. Brings up a real 1-master/2-replica/3-sentinel
Redis cluster to prove `config/settings/production.py`'s `REDIS_SENTINEL_HOSTS`
branch actually works end-to-end, rather than just "the config loads".

```bash
cd scripts/redis-sentinel-verify
docker compose -p sentinel-verify up -d
sleep 5   # let sentinels elect/settle
docker exec sentinel-verify-sentinel-1-1 redis-cli -p 26379 SENTINEL master mymaster

# run the real app's config against the real cluster (PowerShell path shown;
# adjust the -v path for your shell)
docker run --rm --network sentinel-verify_sentinel-net --entrypoint python \
  -v "<repo>/scripts/redis-sentinel-verify/test_sentinel.py:/tmp/test_sentinel.py:ro" \
  csp-balance-tracker:latest /tmp/test_sentinel.py

docker compose -p sentinel-verify down -v   # tear down when done
```

## What this caught (2026-10-06)

`sentinel.conf` needs `sentinel resolve-hostnames yes` + `sentinel
announce-hostnames yes` — Redis Sentinel refuses a hostname (vs. a raw IP)
in `sentinel monitor` otherwise, and fails to start at all.

`production.py`'s Sentinel branch had two real bugs, only found by running
this: `LOCATION` must be `redis://<master-name>/<db>` (the hostname part of
that URL is read as the Sentinel service name — reusing the plain
`REDIS_URL` pointed it at the wrong name entirely), and
`OPTIONS.CONNECTION_FACTORY` must explicitly be
`"django_redis.pool.SentinelConnectionFactory"` (without it, django-redis
silently uses the non-Sentinel connection factory and fails with a
`TypeError`). Both are fixed in `production.py`; this directory is what
proved the fix, and is the reproducible way to re-prove it after any future
change to that branch.
