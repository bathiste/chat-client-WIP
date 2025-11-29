#!/usr/bin/env python3
"""
Automated Commit Bot for Python 3.11+
- Automatically stages, commits, pulls, and pushes changes
- Handles unstaged changes and rebase safely
- Works on Windows, Linux, and macOS
"""

import subprocess
import os
from datetime import datetime

def run_git(cmd, cwd=None):
    """Run git command and return (success, output)"""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, cwd=cwd)
        return True, result.stdout.strip()
    except subprocess.CalledProcessError as e:
        return False, e.stderr.strip()

def get_current_branch():
    success, branch = run_git(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if success:
        return branch
    return "main"

def main():
    print(f"OS detected: {os.name} / {os.sys.platform}")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    commit_msg = f"Commit: {now}"

    # Ensure we are in the script's directory
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    # Stage all changes (new, modified, deleted)
    run_git(["git", "add", "--all"])

    # Commit changes (skip if nothing to commit)
    success, output = run_git(["git", "commit", "-m", commit_msg])
    if success:
        print(f"{commit_msg} committed")
    else:
        if "nothing to commit" in output.lower():
            print("No local changes to commit")
        else:
            print(f"Git commit error: {output}")

    # Determine branch
    branch = get_current_branch()
    print(f"Current branch: {branch}")

    # Pull latest changes safely
    print("Pulling latest changes...")
    success, output = run_git(["git", "pull", "--rebase", "origin", branch])
    if success:
        print("Pull successful")
    else:
        print(f"Git pull error: {output}")
        print("Continuing to push anyway (may fail)")

    # Push to remote
    success, output = run_git(["git", "push", "origin", branch])
    if success:
        print("Push successful ✅")
    else:
        print(f"Push failed: {output}")
        print("You may need to resolve conflicts manually.")

if __name__ == "__main__":
    main()
