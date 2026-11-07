# AWS Lambda Python 3.13 base, pinned to its linux/amd64 manifest
# (public.ecr.aws/lambda/python:3.13 as of 2026-11-01; image created 2026-10-31).
# Build with --platform linux/amd64.
FROM public.ecr.aws/lambda/python:3.13@sha256:2f2661d61213e6690f7613bee9d041592c0e92d977168fe6322077e378ade073

# Runtime dependencies exactly as locked: generated from uv.lock with
#   uv export --frozen --no-dev --no-emit-project --format requirements-txt -o requirements.lock.txt
# Every file must match its hash, nothing is resolved at build time, and only
# wheels are accepted except sgmllib3k (pure Python, published only as an sdist).
COPY requirements.lock.txt .
RUN pip install --no-cache-dir --require-hashes --no-deps \
        --only-binary=:all: --no-binary=sgmllib3k \
        -r requirements.lock.txt -t ${LAMBDA_TASK_ROOT}

COPY . ${LAMBDA_TASK_ROOT}

CMD ["handlers.rss_handler.handler.lambda_handler"]
