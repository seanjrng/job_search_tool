"""Sanity test for ats_clients.py's fetch_smartrecruiters — no real network
calls (this dev sandbox blocks outbound httpx to arbitrary domains, and
api.smartrecruiters.com additionally blocked this session's WebFetch via
robots.txt — see the CONFIDENCE NOTE in ats_clients.py). Mocks httpx.get to
verify: (1) basic field parsing incl. remote-location formatting,
(2) pagination across pages via limit/offset, (3) the full-description
fetch is gated to postings that already pass title/location filters —
same idea as aggregator_clients.fetch_adzuna's gating, so a company with
hundreds of postings doesn't turn into hundreds of extra requests,
(4) a failed detail fetch degrades to an empty description, not a crash,
and (5) "smartrecruiters" is wired into FETCHERS/fetch_company. Run with:
python -m pytest app/tests/test_ats_clients.py
"""
from unittest.mock import patch, MagicMock

from app import ats_clients


def _list_resp(content):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"content": content})
    return resp


def _detail_resp(job_description="", qualifications="", additional=""):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    sections = {}
    if job_description:
        sections["jobDescription"] = {"text": job_description}
    if qualifications:
        sections["qualifications"] = {"text": qualifications}
    if additional:
        sections["additionalInformation"] = {"text": additional}
    resp.json = MagicMock(return_value={"jobAd": {"sections": sections}})
    return resp


def _posting(id="1", name="Software Engineer", city="Calgary", region="AB", country="Canada", remote=False):
    return {
        "id": id, "name": name,
        "location": {"city": city, "region": region, "country": country, "remote": remote},
        "releasedDate": "2026-08-01T00:00:00.000Z",
    }


def test_basic_parsing_and_remote_location():
    posting = _posting(remote=True)
    with patch("httpx.get", side_effect=[_list_resp([posting]), _detail_resp("Build things with Python.")]):
        jobs = ats_clients.fetch_smartrecruiters("TestCo", "testco")
    assert len(jobs) == 1
    j = jobs[0]
    assert j["company"] == "TestCo"
    assert j["title"] == "Software Engineer"
    assert j["location"] == "Remote (Calgary, AB, Canada)"
    assert j["posted_at"] == "2026-08-01T00:00:00.000Z"
    assert j["url"] == "https://jobs.smartrecruiters.com/testco/1"  # fallback construction, no applyUrl/ref given
    assert "Python" in j["description"]
    print("fetch_smartrecruiters: basic parsing + remote location + description fetched — OK")


def test_prefers_applyurl_or_ref_when_present():
    posting = _posting(id="2")
    posting["applyUrl"] = "https://jobs.smartrecruiters.com/TestCo/oa_1234"
    with patch("httpx.get", side_effect=[_list_resp([posting]), _detail_resp("JD text")]):
        jobs = ats_clients.fetch_smartrecruiters("TestCo", "testco")
    assert jobs[0]["url"] == "https://jobs.smartrecruiters.com/TestCo/oa_1234"
    print("fetch_smartrecruiters: prefers applyUrl over constructed URL — OK")


def test_pagination_across_pages():
    page1 = [_posting(id=str(i)) for i in range(100)]  # full page -> expect another request
    page2 = [_posting(id="100")]  # short page -> stop

    # Only postings that pass title/location trigger a detail fetch; all
    # 101 here are "Software Engineer" / Calgary, AB -> all gated in, so
    # interleave list/detail responses accordingly.
    responses = [_list_resp(page1)]
    responses += [_detail_resp("JD") for _ in range(100)]
    responses += [_list_resp(page2)]
    responses += [_detail_resp("JD")]

    with patch("httpx.get", side_effect=responses) as mock_get:
        jobs = ats_clients.fetch_smartrecruiters("TestCo", "testco")

    assert len(jobs) == 101
    # 2 list calls (full page then short page) + 101 detail calls
    assert mock_get.call_count == 2 + 101
    # second list call used offset=100
    second_list_call = mock_get.call_args_list[101]  # after 100 detail calls following the first list call
    assert second_list_call.kwargs["params"]["offset"] == 100
    print("fetch_smartrecruiters: pagination via limit/offset — OK")


def test_description_fetch_gated_to_promising_postings():
    promising = _posting(id="1", name="Software Engineer", city="Calgary", region="AB", country="Canada")
    not_promising_title = _posting(id="2", name="Marketing Operations Manager")
    not_promising_location = _posting(id="3", name="Software Engineer", city="Warsaw", region="", country="Poland")

    with patch("httpx.get", side_effect=[
        _list_resp([promising, not_promising_title, not_promising_location]),
        _detail_resp("Only fetched for the promising one"),
    ]) as mock_get:
        jobs = ats_clients.fetch_smartrecruiters("TestCo", "testco")

    assert mock_get.call_count == 2  # 1 list call + exactly 1 detail call
    by_id = {j["url"].rsplit("/", 1)[-1]: j for j in jobs}
    assert "Only fetched" in by_id["1"]["description"]
    assert by_id["2"]["description"] == ""
    assert by_id["3"]["description"] == ""
    print("fetch_smartrecruiters: full-description fetch gated to promising postings only — OK")


def test_description_fetch_failure_degrades_gracefully():
    posting = _posting()
    with patch("httpx.get", side_effect=[_list_resp([posting]), Exception("500 server error")]):
        jobs = ats_clients.fetch_smartrecruiters("TestCo", "testco")
    assert jobs[0]["description"] == ""
    print("fetch_smartrecruiters: detail-fetch failure -> empty description, no crash — OK")


def _wd_list(postings, total=None):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    body = {"jobPostings": postings}
    if total is not None:
        body["total"] = total
    resp.json = MagicMock(return_value=body)
    return resp


def _wd_detail(description="Build things with Python.", start="2026-10-20", location="Calgary, AB, Canada"):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={
        "jobPostingInfo": {
            "jobDescription": description,
            "startDate": start,
            "location": location,
            "postedOn": "Posted Today",
        }
    })
    return resp


def _wd_posting(path="/job/Calgary/Software-Engineer_JR1", title="Software Engineer", location="Calgary, AB, Canada"):
    return {
        "title": title,
        "externalPath": path,
        "locationsText": location,
        "postedOn": "Posted Today",
        "bulletFields": ["JR1"],
    }


def test_workday_parses_a_posting_and_fetches_its_description():
    slug = "nvidia.wd5/NVIDIAExternalCareerSite"
    with patch("httpx.post", return_value=_wd_list([_wd_posting()], total=1)) as mock_post, \
         patch("httpx.get", return_value=_wd_detail()) as mock_get, \
         patch("app.ats_clients.filters.title_is_relevant", return_value=True), \
         patch("app.ats_clients.filters.location_is_allowed", return_value=True):
        jobs = ats_clients.fetch_workday("NVIDIA", slug)
    assert len(jobs) == 1
    job = jobs[0]
    assert job["company"] == "NVIDIA"
    assert job["title"] == "Software Engineer"
    assert job["location"] == "Calgary, AB, Canada"
    assert job["posted_at"] == "2026-10-20"
    assert job["url"] == (
        "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"
        "/job/Calgary/Software-Engineer_JR1"
    )
    assert "Python" in job["description"]
    assert mock_post.call_args.args[0] == (
        "https://nvidia.wd5.myworkdayjobs.com/wday/cxs/nvidia/NVIDIAExternalCareerSite/jobs"
    )
    assert mock_post.call_args.kwargs["json"] == {
        "appliedFacets": {},
        "limit": 20,
        "offset": 0,
        "searchText": "",
    }
    assert mock_get.call_args.args[0].endswith("/job/Software-Engineer_JR1")


def test_workday_pages_until_offset_reaches_total():
    page = [_wd_posting(path=f"/job/X/Role_JR{i}", title="Marketing Manager", location="Warsaw") for i in range(20)]
    short = [_wd_posting(path="/job/X/Role_JR20", title="Marketing Manager", location="Warsaw")]
    with patch("httpx.post", side_effect=[_wd_list(page, total=21), _wd_list(short, total=21)]) as mock_post, \
         patch("httpx.get") as mock_get:
        jobs = ats_clients.fetch_workday("NVIDIA", "nvidia.wd5/NVIDIAExternalCareerSite")
    assert len(jobs) == 21
    assert mock_get.call_count == 0
    assert mock_post.call_count == 2
    assert mock_post.call_args_list[1].kwargs["json"]["offset"] == 20


def test_workday_description_is_gated_and_a_detail_failure_is_empty():
    promising = _wd_posting()
    other = _wd_posting(path="/job/Warsaw/Role_JR2", title="Software Engineer", location="Warsaw, Poland")
    def relevant(title):
        return title == "Software Engineer"

    def allowed(location):
        return "Calgary" in location

    with patch("httpx.post", return_value=_wd_list([promising, other], total=2)), \
         patch("httpx.get", side_effect=Exception("timeout")) as mock_get, \
         patch("app.ats_clients.filters.title_is_relevant", side_effect=relevant), \
         patch("app.ats_clients.filters.location_is_allowed", side_effect=allowed):
        jobs = ats_clients.fetch_workday("NVIDIA", "nvidia.wd5/NVIDIAExternalCareerSite")
    assert mock_get.call_count == 1
    assert mock_get.call_args.args[0].endswith("/job/Software-Engineer_JR1")
    assert len(jobs) == 2
    by_url = {job["url"]: job for job in jobs}
    promising_url = (
        "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/Calgary/Software-Engineer_JR1"
    )
    assert by_url[promising_url]["description"] == ""
    assert by_url[promising_url]["posted_at"] == "Posted Today"
    other_url = "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite/job/Warsaw/Role_JR2"
    assert by_url[other_url]["description"] == ""


def test_workday_404_uses_myworkdaysite_for_the_list():
    first = MagicMock()
    first.status_code = 404
    first.raise_for_status = MagicMock()
    with patch("httpx.post", side_effect=[first, _wd_list([], total=0)]) as mock_post:
        jobs = ats_clients.fetch_workday("Clio", "clio.wd3/Clio_Careers")
    assert jobs == []
    assert mock_post.call_args_list[0].args[0].startswith("https://clio.wd3.myworkdayjobs.com/")
    assert mock_post.call_args_list[1].args[0] == (
        "https://wd3.myworkdaysite.com/wday/cxs/clio/Clio_Careers/jobs"
    )


def test_workday_is_wired_into_fetch_company():
    assert ats_clients.FETCHERS["workday"] is ats_clients.fetch_workday
    with patch("httpx.post", return_value=_wd_list([], total=0)):
        jobs = ats_clients.fetch_company({
            "name": "NVIDIA",
            "ats": "workday",
            "slug": "nvidia.wd5/NVIDIAExternalCareerSite",
        })
    assert jobs == []


def test_wired_into_fetchers_and_fetch_company():
    assert ats_clients.FETCHERS["smartrecruiters"] is ats_clients.fetch_smartrecruiters
    with patch("httpx.get", side_effect=[_list_resp([]), ]):
        jobs = ats_clients.fetch_company({"name": "TestCo", "ats": "smartrecruiters", "slug": "testco"})
    assert jobs == []
    print("FETCHERS/fetch_company: smartrecruiters wired in correctly — OK")


if __name__ == "__main__":
    test_basic_parsing_and_remote_location()
    test_prefers_applyurl_or_ref_when_present()
    test_pagination_across_pages()
    test_description_fetch_gated_to_promising_postings()
    test_description_fetch_failure_degrades_gracefully()
    test_wired_into_fetchers_and_fetch_company()
    print("\nAll assertions passed.")