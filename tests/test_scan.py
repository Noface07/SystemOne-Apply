"""jev-apply scan: public Greenhouse, Lever and Ashby boards read offline through a mock transport."""

import json

import httpx

from jev_apply import scan


def test_boards_file_lines_name_a_provider_and_board():
    assert scan.board("greenhouse:devrev") == ("greenhouse", "devrev")
    assert scan.board("https://job-boards.greenhouse.io/prodigal/jobs/5208862007") == ("greenhouse", "prodigal")
    assert scan.board("https://jobs.lever.co/Allata/a0eb9210") == ("lever", "Allata")
    assert scan.board("https://jobs.ashbyhq.com/cerebras") == ("ashby", "cerebras")
    assert scan.board("# a comment") is None and scan.board("https://careers.example.com") is None


def test_years_asked_are_read_from_the_description():
    assert scan.min_years("We need 2-4 years of experience with C#, 5+ years preferred") == 2
    assert scan.min_years("Fresh graduates welcome") is None


BOARDS = {
    "boards-api.greenhouse.io": {
        "jobs": [
            {"title": "Software Engineer (.NET)", "location": {"name": "Bengaluru, India"},
             "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/111",
             "content": "&lt;p&gt;1-3 years of C# and .NET experience&lt;/p&gt;"},
            {"title": "Senior .NET Engineer", "location": {"name": "Bengaluru, India"},
             "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/112", "content": "C# .NET"},
            {"title": ".NET Developer", "location": {"name": "Berlin, Germany"},
             "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/113", "content": "C#"},
            {"title": "C# Developer", "location": {"name": "Remote"},
             "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/114", "content": "C#, 5+ years"},
        ]
    },
    "api.lever.co": [
        {"text": "Junior Software Engineer", "categories": {"location": "Vadodara, India"},
         "hostedUrl": "https://jobs.lever.co/allata/abc", "descriptionPlain": "C# and .NET, 6 months+"},
    ],
    "api.ashbyhq.com": {
        "jobs": [
            {"title": "ML Engineer", "location": "Bengaluru, India", "jobUrl": "https://jobs.ashbyhq.com/x/1",
             "applyUrl": "https://jobs.ashbyhq.com/x/1/application", "descriptionPlain": "PyTorch", "isListed": True},
        ]
    },
}  # fmt: skip


def mock_client():
    return httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=BOARDS[request.url.host]))
    )


def test_scan_keeps_matching_jobs_you_qualify_for(tmp_path):
    done = tmp_path / "applied.json"
    done.write_text(json.dumps({"greenhouse:111": {"status": "submitted"}}))
    lines = ["greenhouse:acme", "lever:allata", "ashby:x", "https://careers.example.com"]
    jobs, problems = scan.scan(lines, ["c#", ".net"], "india|remote", 2, done, client=mock_client())
    urls = [j.url for j in jobs]
    assert urls == ["https://jobs.lever.co/allata/abc/apply"]  # 111 applied, 112 senior, 113 Berlin, 114 asks 5+
    assert problems and "careers.example.com" in problems[0]
    everything, _ = scan.scan(lines, ["c#"], "india|remote", 5, None, client=mock_client())
    assert "https://job-boards.greenhouse.io/acme/jobs/114" in [j.url for j in everything]


def test_keywords_are_whole_words_and_remote_means_india_or_anywhere():
    job = scan.Job(
        "greenhouse", "x", "Accounts Payable Analyst", "Bengaluru, India", "u", "Storage and leverage, 1 year"
    )
    assert not scan.fits(job, ["rag", "llm"], scan.INDIA, 2)  # "rag" is not in "storage"
    ml = scan.Job("greenhouse", "x", "ML Engineer", "Remote - USA", "u", "LLM and RAG work")
    assert not scan.fits(ml, ["rag", "llm"], scan.INDIA, 2)  # remote, but in another country
    here = scan.Job("greenhouse", "x", "ML Engineer", "Remote - India", "u", "LLM and RAG work")
    assert scan.fits(here, ["rag", "llm"], scan.INDIA, 2)
    once = scan.Job("greenhouse", "x", "Backend Engineer", "Pune, India", "u", "Some automation scripts")
    assert not scan.fits(once, ["automation", "scada"], scan.INDIA, 2)  # one passing word in the description
    assert scan.fits(scan.Job("lever", "x", "C# Developer", "Hyderabad, IN", "u", ""), ["c#"], scan.INDIA, 2)
