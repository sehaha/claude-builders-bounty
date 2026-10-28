#!/usr/bin/env python3
"""
Claude Code PR Reviewer Sub-Agent
Analyzes GitHub Pull Request diffs and outputs structured Markdown review comments.
Compliant with Claude Builders Bounty #4 acceptance criteria.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

REVIEW_PROMPT_TEMPLATE = """You are an expert senior code reviewer and security auditor.
Analyze the following Pull Request details and diff thoroughly.

PR Title: {title}
PR Author: {author}
Target Branch: {base_ref} <- Source Branch: {head_ref}
PR Description:
{description}

=== GIT DIFF BEGIN ===
{diff}
=== GIT DIFF END ===

You MUST produce a structured code review in strict Markdown format adhering to the following schema:

## 🤖 Claude Code PR Review

### 📋 Summary of Changes
Provide exactly 2 to 3 concise, high-impact sentences summarizing what changed, the architectural intent, and key files touched.

### ⚠️ Identified Risks
Provide a bulleted list of potential risks, security vulnerabilities, edge cases, breaking changes, or concurrency/performance issues. If no critical risks are found, highlight defensive edge cases or test gaps.
- **[Security / Stability / Performance / Edge Case]**: Description of the risk and potential blast radius.

### 💡 Improvement Suggestions
Provide a bulleted list of actionable, concrete suggestions for cleaner code, idiomatic patterns, enhanced test coverage, or documentation.
- **[Refactor / Architecture / Best Practice / Testing]**: Specific actionable suggestion with code pointers where relevant.

### 🎯 Confidence Score
State the confidence rating clearly on the first line:
**Confidence: [High | Medium | Low]**
Follow with 1-2 sentences explaining why this score was assigned based on the diff complexity, test coverage, and blast radius.

Do NOT include any extra introductory or concluding chatter outside of this Markdown structure.
"""

def parse_pr_identifier(pr_arg: str) -> Tuple[str, str, int]:
    """
    Parses PR identifier into (owner, repo, pr_number).
    Accepts:
      - https://github.com/owner/repo/pull/123
      - owner/repo#123
      - 123 (if inside a git repo with a github remote)
    """
    # Pattern 1: Full URL
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_arg)
    if m:
        return m.group(1), m.group(2), int(m.group(3))

    # Pattern 2: owner/repo#123
    m = re.match(r"([^/]+)/([^#]+)#(\d+)", pr_arg)
    if m:
        return m.group(1), m.group(2), int(m.group(3))

    # Pattern 3: PR number alone
    if pr_arg.isdigit():
        pr_num = int(pr_arg)
        try:
            res = subprocess.run(["git", "config", "--get", "remote.origin.url"], capture_output=True, text=True, check=True)
            url = res.stdout.strip()
            # Match ssh or https
            m = re.search(r"github\.com[:/]([^/]+)/([^/.]+)(?:\.git)?", url)
            if m:
                return m.group(1), m.group(2), pr_num
        except Exception:
            pass

    raise ValueError(f"Could not parse GitHub PR identifier from: '{pr_arg}'. Expected 'https://github.com/owner/repo/pull/<num>'")

def get_pr_metadata_and_diff(owner: str, repo: str, pr_num: int) -> Tuple[Dict[str, Any], str]:
    """Fetches PR metadata and raw diff using gh CLI if available, else GitHub REST API."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    
    # Try gh CLI first
    if shutil.which("gh"):
        try:
            meta_res = subprocess.run(
                ["gh", "pr", "view", str(pr_num), "--repo", f"{owner}/{repo}", "--json", "title,author,baseRefName,headRefName,body"],
                capture_output=True, text=True, check=True
            )
            meta = json.loads(meta_res.stdout)
            
            diff_res = subprocess.run(
                ["gh", "pr", "diff", str(pr_num), "--repo", f"{owner}/{repo}"],
                capture_output=True, text=True, check=True
            )
            diff = diff_res.stdout
            return {
                "title": meta.get("title", ""),
                "author": meta.get("author", {}).get("login", "unknown"),
                "base_ref": meta.get("baseRefName", "main"),
                "head_ref": meta.get("headRefName", "branch"),
                "description": meta.get("body", "") or "No description provided."
            }, diff
        except Exception as e:
            # Fall back to REST API
            pass

    # GitHub REST API Fallback
    headers = {
        "User-Agent": "Claude-Code-PR-Reviewer",
        "Accept": "application/vnd.github.v3+json"
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_num}"
    req = urllib.request.Request(api_url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        pr_data = json.loads(resp.read().decode("utf-8"))

    # Fetch diff
    diff_headers = headers.copy()
    diff_headers["Accept"] = "application/vnd.github.v3.diff"
    diff_req = urllib.request.Request(api_url, headers=diff_headers)
    with urllib.request.urlopen(diff_req, timeout=30) as resp:
        diff_text = resp.read().decode("utf-8", errors="replace")

    metadata = {
        "title": pr_data.get("title", ""),
        "author": pr_data.get("user", {}).get("login", "unknown"),
        "base_ref": pr_data.get("base", {}).get("ref", "main"),
        "head_ref": pr_data.get("head", {}).get("ref", "branch"),
        "description": pr_data.get("body", "") or "No description provided."
    }
    return metadata, diff_text

def truncate_diff_if_needed(diff: str, max_chars: int = 40000) -> str:
    """Truncates very large diffs gracefully while preserving file headers."""
    if len(diff) <= max_chars:
        return diff
    
    lines = diff.splitlines()
    truncated_lines = []
    current_chars = 0
    
    for line in lines:
        if current_chars + len(line) + 1 > max_chars:
            truncated_lines.append("\n... [Diff truncated due to size. Focused on initial modified files] ...")
            break
        truncated_lines.append(line)
        current_chars += len(line) + 1
        
    return "\n".join(truncated_lines)

def run_claude_review(prompt: str, lane_id: Optional[int] = None, model: str = "claude-3-5-sonnet-20241022") -> str:
    """
    Executes Claude review via:
    1. Local Lane Manager (if lane_id is set or lane module available)
    2. Anthropic API / LiteLLM / Gateway if configured
    3. Official Claude CLI ('claude -p')
    """
    # 1. Check if lane_manager is available and lane_id specified
    if lane_id is not None:
        try:
            # Check sys.path or hansong bounty root
            for p in ["/Users/eiden/hansong", str(Path.cwd()), str(Path.cwd().parent)]:
                if p not in sys.path:
                    sys.path.insert(0, p)
            from bounty.lane_manager import execute_lane
            res = execute_lane(lane_id, prompt, model="sonnet", timeout=180)
            if res.get("success") and res.get("reply"):
                return res["reply"].strip()
        except Exception as e:
            # Fall back
            pass

    # 2. Check Gateway / LiteLLM / Anthropic API environment
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    api_base = os.environ.get("ANTHROPIC_BASE_URL") or os.environ.get("ANTHROPIC_API_BASE")
    
    # Check if local gateway is running at port 8000
    if not api_base:
        try:
            req = urllib.request.Request("http://127.0.0.1:8000/health")
            with urllib.request.urlopen(req, timeout=1) as resp:
                if resp.status == 200:
                    api_base = "http://127.0.0.1:8000"
        except Exception:
            pass

    if api_base:
        url = f"{api_base.rstrip('/')}/v1/messages"
        payload = {
            "model": model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}]
        }
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01"
        }
        if api_key:
            headers["x-api-key"] = api_key
        if "127.0.0.1:8000" in api_base:
            headers["x-si-mode"] = "local"

        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                for block in data.get("content", []):
                    if block.get("type") == "text":
                        return block.get("text", "").strip()
        except Exception as e:
            pass

    # 3. Direct Claude CLI fallback
    claude_cmd = shutil.which("claude") or "/opt/homebrew/bin/claude"
    if claude_cmd and os.path.exists(claude_cmd):
        cmd = [
            claude_cmd,
            "-p", prompt,
            "--no-session-persistence",
            "--dangerously-skip-permissions"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()

    raise RuntimeError("Failed to invoke Claude review: No Claude CLI, local lane, or API endpoint available.")

def post_comment_to_pr(owner: str, repo: str, pr_num: int, comment_body: str) -> bool:
    """Posts review comment back to GitHub PR."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    
    # Try gh CLI
    if shutil.which("gh"):
        try:
            subprocess.run(
                ["gh", "pr", "comment", str(pr_num), "--repo", f"{owner}/{repo}", "--body", comment_body],
                check=True, capture_output=True
            )
            return True
        except Exception:
            pass

    # Fallback to GitHub REST API
    if token:
        api_url = f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_num}/comments"
        headers = {
            "User-Agent": "Claude-Code-PR-Reviewer",
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json"
        }
        data = json.dumps({"body": comment_body}).encode("utf-8")
        req = urllib.request.Request(api_url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status in (200, 201)
        except Exception as e:
            print(f"[!] Warning: Failed to post comment via API: {e}", file=sys.stderr)
            return False

    return False

def main():
    parser = argparse.ArgumentParser(
        description="Claude Code PR Reviewer Sub-Agent — Analyzes PR diffs and outputs structured Markdown reviews."
    )
    parser.add_argument("--pr", required=True, help="GitHub PR URL (e.g., https://github.com/owner/repo/pull/123) or identifier")
    parser.add_argument("--post", action="store_true", help="Post review comment directly back to the GitHub PR")
    parser.add_argument("--lane", type=int, default=None, help="ComputeJuicer / Multi-Lane account ID to execute on (e.g. 1)")
    parser.add_argument("--output", "-o", help="Optional filepath to save the review markdown report")
    parser.add_argument("--model", default="claude-3-5-sonnet-20241022", help="Claude model name")
    args = parser.parse_args()

    try:
        owner, repo, pr_num = parse_pr_identifier(args.pr)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[*] Fetching PR details for {owner}/{repo}#{pr_num}...", file=sys.stderr)
    try:
        meta, diff = get_pr_metadata_and_diff(owner, repo, pr_num)
    except Exception as e:
        print(f"Error fetching PR data: {e}", file=sys.stderr)
        sys.exit(1)

    if not diff.strip():
        print("Error: PR diff is empty or cannot be retrieved.", file=sys.stderr)
        sys.exit(1)

    clean_diff = truncate_diff_if_needed(diff)
    prompt = REVIEW_PROMPT_TEMPLATE.format(
        title=meta["title"],
        author=meta["author"],
        base_ref=meta["base_ref"],
        head_ref=meta["head_ref"],
        description=meta["description"],
        diff=clean_diff
    )

    print(f"[*] Analyzing PR diff with Claude (Lane: {args.lane if args.lane is not None else 'Default'})...", file=sys.stderr)
    try:
        review = run_claude_review(prompt, lane_id=args.lane, model=args.model)
    except Exception as e:
        print(f"Error during review generation: {e}", file=sys.stderr)
        sys.exit(1)

    # Output to stdout
    print(review)

    # Optional file output
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(review, encoding="utf-8")
        print(f"[+] Review saved to {out_path}", file=sys.stderr)

    # Optional PR comment posting
    if args.post:
        print(f"[*] Posting review comment to {owner}/{repo}#{pr_num}...", file=sys.stderr)
        if post_comment_to_pr(owner, repo, pr_num, review):
            print(f"[+] Successfully posted review to PR #{pr_num}!", file=sys.stderr)
        else:
            print("[!] Could not post comment. Check GITHUB_TOKEN permissions or gh auth.", file=sys.stderr)

if __name__ == "__main__":
    main()
