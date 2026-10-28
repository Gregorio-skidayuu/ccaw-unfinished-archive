import argparse

from .runner import run_worker


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True)
    parser.add_argument("job_id")
    args = parser.parse_args()
    run_worker(args.home, args.job_id)
