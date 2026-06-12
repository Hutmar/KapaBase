import subprocess
import sys
import requests
import time

# =======================
# CONFIG
# =======================
SERVICE_NAME = "kapaBase.service"
HEALTHCHECK_URL = "http://localhost:8000/planning_status?task_ids=1"
BRANCH = "main"
RESTART_CMD = ["sudo", "systemctl", "restart", SERVICE_NAME]

# =======================
# HELPERS
# =======================
def run(cmd, check=True):
    print(f"→ {' '.join(cmd)}")
    result = subprocess.run(cmd, text=True, capture_output=True)
    print(result.stdout)
    if result.returncode != 0 and check:
        print(result.stderr)
        raise RuntimeError(f"Command failed: {' '.join(cmd)}")
    return result


def git(cmd):
    return run(["git"] + cmd)


# =======================
# STEP 1: FETCH & CHECK
# =======================
def remote_changes_exist():
    git(["fetch"])
    local = run(["git", "rev-parse", "HEAD"]).stdout.strip()
    remote = run(["git", "rev-parse", f"origin/{BRANCH}"]).stdout.strip()
    return local != remote


# =======================
# STEP 2: STASH LOCAL CHANGES
# =======================
def stash_changes():
    print("Stashing local changes...")
    result = git(["stash", "push", "-u"])
    return "No local changes" not in result.stdout


def restore_stash(had_stash):
    if had_stash:
        print("Restoring stash...")
        git(["stash", "pop"])


# =======================
# STEP 3: UPDATE CODE
# =======================
def update_code():
    git(["checkout", BRANCH])
    git(["pull", "--ff-only"])


# =======================
# STEP 4: RESTART SERVICE
# =======================
def restart_service():
    run(RESTART_CMD)


# =======================
# STEP 5: HEALTH CHECK
# =======================
def health_check():
    print(f"Checking {HEALTHCHECK_URL}")
    try:
        r = requests.get(HEALTHCHECK_URL, timeout=10)
        print(f"Status: {r.status_code}")
        return r.status_code == 200
    except Exception as e:
        print(f"Healthcheck failed: {e}")
        return False


# =======================
# ROLLBACK
# =======================
def rollback(old_commit, had_stash):
    print("!!! ROLLBACK INITIATED !!!")

    git(["reset", "--hard", old_commit])

    if had_stash:
        restore_stash(True)

    run(RESTART_CMD)

    print("Rollback complete.")


# =======================
# MAIN
# =======================
def main():
    try:
        print("Saving current commit...")
        old_commit = run(["git", "rev-parse", "HEAD"]).stdout.strip()

        print("Checking remote changes...")
        if not remote_changes_exist():
            print("No remote changes detected.")
            return

        print("Remote changes detected.")

        had_stash = stash_changes()

        print("Updating code...")
        update_code()

        print("Restarting service...")
        restart_service()

        time.sleep(2)

        if health_check():
            print("Deployment successful ✔")
            return

        rollback(old_commit, had_stash)
        print("Deployment failed ❌")

    except Exception as e:
        print(f"ERROR: {e}")
        print("Attempting rollback...")

        try:
            rollback(old_commit, True)
        except Exception as e2:
            print(f"Rollback also failed: {e2}")

        sys.exit(1)


if __name__ == "__main__":
    main()