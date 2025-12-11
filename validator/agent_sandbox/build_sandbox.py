import os
from dotenv import load_dotenv
from python_on_whales import docker

load_dotenv()

username = os.getenv("GHCR_USERNAME")
token = os.getenv("GHCR_TOKEN")
version = os.getenv("SANDBOX_VERSION", "latest")

if not username or not token:
    raise ValueError("Missing GHCR_USERNAME, GHCR_TOKEN in .env")

IMAGE = f"ghcr.io/{username}/agent-sandbox:{version}"

docker.login(server="ghcr.io", username=username, password=token)
docker.build(".", tags=[IMAGE], platforms=["linux/amd64", "linux/arm64"])
docker.push(IMAGE)

print(IMAGE)
