"""Native launcher: one self-contained worker, or an affinity-sized Redis deployment."""

import os


def worker_count() -> int:
    configured = os.environ.get("WEB_WORKERS")
    if configured:
        workers = int(configured)
    elif os.environ.get("REDIS_URL"):
        # process_cpu_count respects the CPU affinity mask (container cpusets).
        cpus = os.process_cpu_count() or 1
        workers = min(4, max(1, cpus))
    else:
        workers = 1
    if workers < 1:
        raise ValueError("WEB_WORKERS must be positive")
    if workers > 1 and not os.environ.get("REDIS_URL"):
        raise ValueError("REDIS_URL is required for multiple workers")
    return workers


def main() -> None:
    import uvicorn

    from . import cli

    workers = worker_count()
    cli.prepare()
    uvicorn.run(
        "campfire.asgi:app",
        host=os.environ.get("BIND", "0.0.0.0"),
        port=int(os.environ.get("HTTP_PORT", "8080")),
        workers=workers,
        loop="uvloop",
        http="httptools",
        ws="websockets",
        access_log=False,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("TRUSTED_PROXIES", "127.0.0.1"),
    )


if __name__ == "__main__":
    main()
