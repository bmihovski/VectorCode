import asyncio
import hashlib
import os
import socket
import subprocess
import sys
from typing import Any, AsyncGenerator, Coroutine

import chromadb
import httpx
from chromadb.api import AsyncClientAPI
from chromadb.api.models.AsyncCollection import AsyncCollection
from chromadb.config import Settings
from chromadb.utils import embedding_functions

from vectorcode.cli_utils import Config, expand_path


async def get_collections(
    client: AsyncClientAPI,
) -> AsyncGenerator[AsyncCollection, None]:
    for collection_name in await client.list_collections():
        collection = await client.get_collection(collection_name, None)
        meta = collection.metadata
        if meta is None:
            continue
        if meta.get("created-by") != "VectorCode":
            continue
        if meta.get("username") not in (
            os.environ.get("USER"),
            os.environ.get("USERNAME"),
            "DEFAULT_USER",
        ):
            continue
        if meta.get("hostname") != socket.gethostname():
            continue
        yield collection


async def try_server(host: str, port: int):
    url = f"http://{host}:{port}/api/v1/heartbeat"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url=url)
            return response.status_code == 200
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return False


async def wait_for_server(host, port, timeout=10):
    # Poll the server until it's ready or timeout is reached
    url = f"http://{host}:{port}/api/v1/heartbeat"
    start_time = asyncio.get_event_loop().time()
    async with httpx.AsyncClient() as client:
        while True:
            try:
                response = await client.get(url)
                if response.status_code == 200:
                    return
            except httpx.RequestError:
                pass  # Server is not yet ready

            if asyncio.get_event_loop().time() - start_time > timeout:
                raise TimeoutError(f"Server did not start within {timeout} seconds.")

            await asyncio.sleep(0.1)  # Wait before retrying


async def start_server(configs: Config):
    assert configs.db_path is not None
    db_path = os.path.expanduser(configs.db_path)
    if not os.path.isdir(db_path):
        print(
            f"Using local database at {os.path.expanduser('~/.local/share/vectorcode/chromadb/')}.",
            file=sys.stderr,
        )
        db_path = os.path.expanduser("~/.local/share/vectorcode/chromadb/")
    env = os.environ.copy()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))  # OS selects a free ephemeral port
        configs.port = int(s.getsockname()[1])
    env.update({"ANONYMIZED_TELEMETRY": "False"})
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "chromadb.cli.cli",
        "run",
        "--host",
        "localhost",
        "--port",
        str(configs.port),
        "--path",
        db_path,
        "--log-path",
        os.path.join(str(configs.project_root), "chroma.log"),
        stdout=subprocess.DEVNULL,
        stderr=sys.stderr,
        env=env,
    )

    await wait_for_server(configs.host, configs.port)
    return process


def get_client(configs: Config) -> Coroutine[Any, Any, AsyncClientAPI]:
    assert configs.host is not None
    assert configs.port is not None
    assert configs.db_path is not None
    settings = {"anonymized_telemetry": False}
    if isinstance(configs.db_settings, dict):
        valid_settings = {
            k: v for k, v in configs.db_settings.items() if k in Settings.__fields__
        }
        settings.update(valid_settings)
    return chromadb.AsyncHttpClient(
        host=configs.host or "localhost",
        port=configs.port or 8000,
        settings=Settings(**settings),
    )


def get_collection_name(full_path: str) -> str:
    full_path = str(expand_path(full_path, absolute=True))
    hasher = hashlib.sha256()
    hasher.update(
        f"{os.environ.get('USER', os.environ.get('USERNAME', 'DEFAULT_USER'))}@{socket.gethostname()}:{full_path}".encode()
    )
    collection_id = hasher.hexdigest()[:63]
    return collection_id


def get_embedding_function(configs: Config) -> chromadb.EmbeddingFunction:
    try:
        return getattr(embedding_functions, configs.embedding_function)(
            **configs.embedding_params
        )
    except AttributeError:
        print(
            f"Failed to use {configs.embedding_function}. Falling back to Sentence Transformer.",
            file=sys.stderr,
        )
        return embedding_functions.SentenceTransformerEmbeddingFunction()


async def get_collection(
    client: AsyncClientAPI, configs: Config, make_if_missing: bool = False
):
    """
    Raise ValueError when make_if_missing is False and no collection is found;
    Raise IndexError on hash collision.
    """
    full_path = str(expand_path(str(configs.project_root), absolute=True))
    collection_name = get_collection_name(full_path)
    embedding_function = get_embedding_function(configs)
    collection_meta = {
        "path": full_path,
        "hostname": socket.gethostname(),
        "created-by": "VectorCode",
        "username": os.environ.get("USER", os.environ.get("USERNAME", "DEFAULT_USER")),
        "embedding_function": configs.embedding_function,
    }

    if not make_if_missing:
        return await client.get_collection(collection_name, embedding_function)
    collection = await client.get_or_create_collection(
        collection_name,
        metadata=collection_meta,
        embedding_function=embedding_function,
    )
    if (
        not collection.metadata.get("hostname") == socket.gethostname()
        or collection.metadata.get("username")
        not in (os.environ.get("USER"), os.environ.get("USERNAME"), "DEFAULT_USER")
        or not collection.metadata.get("created-by") == "VectorCode"
    ):
        raise IndexError(
            "Failed to create the collection due to hash collision. Please file a bug report."
        )
    return collection


def verify_ef(collection: AsyncCollection, configs: Config):
    collection_ef = collection.metadata.get("embedding_function")
    collection_ep = collection.metadata.get("embedding_params")
    if collection_ef and collection_ef != configs.embedding_function:
        print(f"The collection was embedded using {collection_ef}.")
        print(
            "Embeddings and query must use the same embedding function and parameters. Please double-check your config."
        )
        return False
    elif collection_ep and collection_ep != configs.embedding_params:
        print(
            f"The collection was embedded with a different set of configurations: {collection_ep}.",
            file=sys.stderr,
        )
        print("The result may be inaccurate.", file=sys.stderr)
    return True
