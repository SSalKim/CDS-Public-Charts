"""Keep publication on GitHub and discover an idle dedicated compute helper."""
import argparse
import json
import os
from urllib.request import Request, urlopen


def choose(payload, vtg_payload=None):
    if vtg_payload is not None:
        for runner in vtg_payload.get("runners", []):
            labels = {str(label.get("name", "")).lower() for label in runner.get("labels", [])}
            if runner.get("busy") and "cds-vtg" in labels:
                return "ubuntu-24.04"
    for runner in payload.get("runners", []):
        labels = {str(label.get("name", "")).lower() for label in runner.get("labels", [])}
        if runner.get("status") == "online" and not runner.get("busy", True) and "linux" in labels:
            if "cds-public-charts" in labels:
                return ["self-hosted", "linux", "cds-public-charts"]
            if "cds-vtg" in labels:
                return ["self-hosted", "linux", "cds-vtg"]
    return "ubuntu-24.04"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("auto", "github-hosted"), default="auto")
    args = parser.parse_args()
    result = "ubuntu-24.04"
    if args.mode == "auto":
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        token = os.environ.get("GH_TOKEN", "")
        if repository and token:
            try:
                def runners(repo):
                    request = Request(f"https://api.github.com/repos/{repo}/actions/runners?per_page=100",
                                      headers={"Authorization": "Bearer " + token,
                                               "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
                    with urlopen(request, timeout=20) as response:
                        return json.load(response)
                result = choose(runners(repository), runners("SSalKim/CDS"))
            except Exception as exception:
                print("Runner status unavailable; using GitHub-hosted.", type(exception).__name__, getattr(exception, "code", None))
    helper = isinstance(result,list) and "cds-public-charts" in result
    print("GitHub coordinator; optional compute helper:", json.dumps(result) if helper else "unavailable")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write('labels="ubuntu-24.04"\n')
            stream.write("helper="+str(helper).lower()+"\n")


if __name__ == "__main__":
    main()
