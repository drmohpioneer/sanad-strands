FROM public.ecr.aws/docker/library/python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /usr/local/bin/uv
WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --only-group dev --no-install-project && uv build --wheel --no-build-isolation --out-dir /wheels && uv export --locked --no-dev --no-emit-project --format requirements-txt --output-file /requirements.txt
RUN pip install --no-cache-dir --prefix=/runtime --require-hashes -r /requirements.txt && pip install --no-cache-dir --no-deps --prefix=/runtime /wheels/*.whl

FROM public.ecr.aws/docker/library/python:3.12-slim
COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:0.9.1 /lambda-adapter /opt/extensions/lambda-adapter
COPY --from=build /runtime /usr/local
ENV PORT=8080 AWS_LWA_READINESS_CHECK_PATH=/health AWS_LWA_PASS_THROUGH_PATH=/events AWS_LWA_ERROR_STATUS_CODES=500-599 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
CMD ["uvicorn", "sanad.api.lambda_entry:create_runtime_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
