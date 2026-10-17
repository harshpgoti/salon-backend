FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS build
WORKDIR /build
COPY pyproject.toml constraints.txt ./
COPY src ./src
RUN python -m pip install --no-cache-dir --upgrade pip==26.2.1 && python -m pip wheel --no-cache-dir --constraint constraints.txt --wheel-dir /wheels .

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 10001 salon
COPY --from=build /wheels /wheels
COPY constraints.txt /app/constraints.txt
COPY scripts/check_dependency_lock.py /app/scripts/check_dependency_lock.py
RUN python -m pip install --no-cache-dir --upgrade pip==26.2.1 && python -m pip install --no-cache-dir --no-index --find-links=/wheels --constraint /app/constraints.txt /wheels/* && python -m pip check && python /app/scripts/check_dependency_lock.py && rm -rf /wheels
WORKDIR /app
USER salon
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"
CMD ["python", "-m", "uvicorn", "modules.identity.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
