"""Disposable DynamoDB Local process; explicit loopback-only client with dummy credentials."""

import socket
import subprocess
import tempfile
import time
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO, Self

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import (  # type: ignore[import-untyped]
    EndpointConnectionError,
    ReadTimeoutError,
)


class DynamoDBLocal:
    def __init__(
        self, *, repo_root: Path | None = None, java: Path | None = None, jar: Path | None = None
    ):
        root = repo_root or Path(__file__).resolve().parents[3]
        self.java = java or root / ".tools/amazon-corretto-21.jdk/Contents/Home/bin/java"
        self.jar = jar or root / ".tools/dynamodb-local/DynamoDBLocal.jar"
        self.process: subprocess.Popen[bytes] | None = None
        self.client: Any = None
        self.port: int | None = None
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._log: BinaryIO | None = None

    @property
    def available(self) -> bool:
        return (
            self.java.is_file()
            and self.jar.is_file()
            and (self.jar.parent / "DynamoDBLocal_lib").is_dir()
        )

    def __enter__(self) -> Self:
        if not self.available:
            raise FileNotFoundError("DynamoDB Local requires Java and jar/lib under .tools/")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self._temporary = tempfile.TemporaryDirectory(prefix="sanad-ddb-")
        try:
            self._log = (Path(self._temporary.name) / "startup.log").open("w+b")
            self.process = subprocess.Popen(
                [
                    str(self.java.resolve()),
                    f"-Djava.library.path={(self.jar.parent / 'DynamoDBLocal_lib').resolve()}",
                    "-jar",
                    str(self.jar.resolve()),
                    "-inMemory",
                    "-sharedDb",
                    "-disableTelemetry",
                    "-port",
                    str(self.port),
                ],
                cwd=self._temporary.name,
                stdout=self._log,
                stderr=subprocess.STDOUT,
            )
            self.client = boto3.session.Session(
                aws_access_key_id="local", aws_secret_access_key="local", region_name="us-east-1"
            ).client(
                "dynamodb",
                endpoint_url=f"http://127.0.0.1:{self.port}",
                config=Config(
                    connect_timeout=1, read_timeout=1, proxies={}, retries={"max_attempts": 0}
                ),
            )
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError(
                        f"DynamoDB Local exited with code {self.process.returncode}: "
                        + self._diagnostic()
                    )
                try:
                    self.client.list_tables(Limit=1)
                    return self
                except (EndpointConnectionError, ReadTimeoutError):
                    time.sleep(0.1)
            raise TimeoutError("DynamoDB Local readiness timeout: " + self._diagnostic())
        except BaseException:
            self.close()
            raise

    def _diagnostic(self) -> str:
        if self._log is None:
            return "no process log"
        self._log.seek(0)
        return self._log.read(4096).decode("utf-8", errors="replace")

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self._log is not None:
            self._log.close()
        if self._temporary is not None:
            self._temporary.cleanup()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
