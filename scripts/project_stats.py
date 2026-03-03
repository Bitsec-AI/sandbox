import os
import json
import csv
import sys
from pathlib import Path
from loggers.logger import get_logger

logger = get_logger()

REPO_ROOT = os.path.abspath(os.getcwd())
PROJECTS_DIR = os.path.join(REPO_ROOT, "projects")
CURATED_FILE = os.path.join(REPO_ROOT, "validator", "curated-highs-only-2025-08-08.json")

# Smart contract file patterns
PATTERNS = ['**/*.sol', '**/*.vy', '**/*.cairo', '**/*.rs', '**/*.move']

def read_scope_file(project_path: Path, filename: str) -> set[str]:
    """
    Read a scope file (scope.txt or out_of_scope.txt) and return normalized paths.

    Args:
        project_path: Path to the project directory
        filename: Name of the scope file to read

    Returns:
        Set of normalized file paths relative to project root
    """
    scope_file = project_path / filename
    if not scope_file.exists():
        return set()

    paths = set()
    with open(scope_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                # Normalize path: remove leading ./ and convert to Path
                normalized = line.lstrip('./')
                paths.add(normalized)

    return paths

def is_file_in_scope(file_path: Path, project_path: Path,
                     in_scope: set[str], out_of_scope: set[str]) -> bool:
    """
    Determine if a file is in scope based on scope.txt and out_of_scope.txt.

    Rules:
    - If file is in out_of_scope.txt, it's OUT of scope
    - If scope.txt exists and file is in scope.txt, it's IN scope
    - If scope.txt exists and file is NOT in scope.txt, it's OUT of scope
    - If scope.txt doesn't exist, file is IN scope (by default)

    Args:
        file_path: Path to the file to check
        project_path: Path to the project directory
        in_scope: Set of paths from scope.txt
        out_of_scope: Set of paths from out_of_scope.txt

    Returns:
        True if file is in scope, False otherwise
    """
    # Get relative path from project root
    try:
        rel_path = str(file_path.relative_to(project_path))
    except ValueError:
        # File is not under project_path
        return False

    # Check if explicitly out of scope
    if rel_path in out_of_scope:
        return False

    # If scope.txt exists, only files listed there are in scope
    # If scope.txt doesn't exist, all files (except out_of_scope) are in scope
    scope_file_exists = (project_path / "scope.txt").exists()

    if scope_file_exists:
        return rel_path in in_scope
    else:
        # No scope.txt means everything is in scope (except out_of_scope)
        return True

def count_project_files(project_path: Path) -> int:
    """
    Count smart contract files in a project that are in scope.

    Args:
        project_path: Path to the project directory

    Returns:
        Number of in-scope smart contract files
    """
    # Read scope files
    in_scope = read_scope_file(project_path, "scope.txt")
    out_of_scope = read_scope_file(project_path, "out_of_scope.txt")

    # Find all smart contract files
    files = []
    for pattern in PATTERNS:
        files.extend(project_path.glob(pattern))

    # Remove duplicates and filter to files only
    files = set(f for f in files if f.is_file())

    # Count in-scope files
    count = 0
    for file_path in files:
        if is_file_in_scope(file_path, project_path, in_scope, out_of_scope):
            count += 1

    return count

def load_vulnerability_counts():
    """
    Load vulnerability counts from the curated highs file.

    Returns:
        Dictionary mapping project_id to number of vulnerabilities
    """
    vuln_counts = {}

    if not os.path.exists(CURATED_FILE):
        logger.warning(f"Curated file not found: {CURATED_FILE}")
        return vuln_counts

    try:
        with open(CURATED_FILE, 'r', encoding='utf-8') as f:
            projects = json.load(f)

        for project in projects:
            project_id = project.get('project_id')
            vulnerabilities = project.get('vulnerabilities', [])
            if project_id:
                vuln_counts[project_id] = len(vulnerabilities)

    except Exception as e:
        logger.error(f"Error loading curated file: {e}")

    return vuln_counts

def generate_project_stats():
    """
    Generate file count statistics for all projects in the projects/ directory.
    """
    projects_path = Path(PROJECTS_DIR)

    if not projects_path.exists():
        logger.error(f"Projects directory not found: {PROJECTS_DIR}")
        return

    # Get all project directories
    project_dirs = [d for d in projects_path.iterdir() if d.is_dir()]

    if not project_dirs:
        logger.warning("No project directories found")
        return

    logger.info(f"Analyzing {len(project_dirs)} projects...")

    # Load vulnerability counts
    vuln_counts = load_vulnerability_counts()

    # Count files for all projects first
    project_counts = []
    total_files = 0
    total_vulns = 0
    for project_dir in project_dirs:
        project_name = project_dir.name
        file_count = count_project_files(project_dir)
        vuln_count = vuln_counts.get(project_name, 0)
        project_counts.append((project_name, file_count, vuln_count))
        total_files += file_count
        total_vulns += vuln_count

    # Sort by file count (ascending order)
    project_counts.sort(key=lambda x: x[1])

    # Print results
    print("\n" + "="*90)
    print(f"{'Project':<60} {'Files':>10} {'Vulns':>10}")
    print("="*90)

    for project_name, file_count, vuln_count in project_counts:
        # Truncate long project names to fit in the table
        display_name = project_name if len(project_name) <= 60 else project_name[:57] + "..."
        print(f"{display_name:<60} {file_count:>10} {vuln_count:>10}")

    print("="*90)
    print(f"{'TOTAL':<60} {total_files:>10} {total_vulns:>10}")
    print("="*90 + "\n")

    logger.info(f"Analysis complete. Total files: {total_files}, Total vulnerabilities: {total_vulns}")

if __name__ == '__main__':
    generate_project_stats()
