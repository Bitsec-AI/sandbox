import os
import json
import requests
import zipfile
import shutil
from pathlib import Path

from loggers.logger import get_logger

logger = get_logger()

REPO_ROOT = str(Path(__file__).resolve().parents[1])
PROJECTS_FILE = os.path.join(REPO_ROOT, "miner", "projects.json")
PROJECTS_DIR = os.path.join(REPO_ROOT, "projects")


def download_zip(project):
    """
    Download the GitHub zip archive for a specific commit.
    Returns the path to the downloaded zip file.
    """
    project_key = project["project_key"]
    repo_url = project.get("repo_url")
    commit = project.get("commit") or "main"

    if not repo_url:
        raise ValueError(f"Missing repo_url for project {project_key}")

    project_dir = os.path.join(PROJECTS_DIR, project_key)
    zip_path = project_dir + ".zip"

    if os.path.exists(project_dir):
        logger.info(f"⏭️  Skipping download for {project_key} (already exists)")
        return zip_path

    zip_url = f"{repo_url}/archive/{commit}.zip"
    logger.info(f"⬇️  Downloading {project_key} from {zip_url}...")

    with requests.get(zip_url, stream=True) as r:
        r.raise_for_status()
        with open(zip_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

    return zip_path

def extract_zip(zip_path):
    """
    Extract the zip file for a specific codebase.
    Handles atomic extraction with a temporary folder.
    """
    project_dir = os.path.splitext(zip_path)[0]
    project_key = os.path.basename(project_dir)
    temp_dir = project_dir + "_tmp"

    # Skip if already extracted
    if os.path.exists(project_dir):
        logger.info(f"⏭️  Skipping extraction for {project_key} (already exists)")
        
        if os.path.exists(zip_path):
            os.remove(zip_path)

        return

    try:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)

        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(temp_dir)

        # GitHub zip creates a subfolder like repo-commit/, move it up
        subfolders = os.listdir(temp_dir)
        if len(subfolders) == 1:
            extracted_root = os.path.join(temp_dir, subfolders[0])
            shutil.move(extracted_root, project_dir)
            shutil.rmtree(temp_dir)

        else:
            os.rename(temp_dir, project_dir)

        os.remove(zip_path)

        logger.info(f"✅ Extracted into {project_dir}")

    except Exception as e:
        logger.info(f"❌ Failed extraction for {project_key}: {e}")
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)

def init_project(project):
    zip_path = download_zip(project)
    extract_zip(zip_path)

def fetch_projects():
    os.makedirs(PROJECTS_DIR, exist_ok=True)

    with open(PROJECTS_FILE, "r", encoding="utf-8") as f:
        projects = json.load(f)

    logger.info(f"Starting fetching for {len(projects)} projects")
    for project in projects:
        init_project(project)

    logger.info("Finished fetching")
