import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hunnu_harness.paths import TEMP_DIR
from hunnu_harness.browser.commands import (
    BrowserActionResult,
    BrowserObservation,
    NavigateCommand,
    ObservationUnavailable,
    ObserveCommand,
    PageHandle,
    SessionHandle,
)
import hunnu_harness.literature.institutional as institutional_module
from hunnu_harness.literature.adapters.base import SourceActionRequired, SourceLayoutChanged
from hunnu_harness.literature.adapters.springerlink import SpringerLinkAdapter
from hunnu_harness.literature.artifacts import LiteratureArtifactWriter
from hunnu_harness.literature.institutional import (
    HUNNUInstitutionalAccessResolver,
    InstitutionalResolutionTrigger,
    InstitutionalRouteResult,
    InstitutionalRouteStep,
)
from hunnu_harness.literature.models import (
    AccessDecision,
    AccessType,
    LiteratureRecord,
    LiteratureSearchRequest,
    PublicationStatus,
    RunStatus,
)
from hunnu_harness.literature.security import scan_text_for_sensitive_leaks
from hunnu_harness.literature.workflow import (
    LiteratureAcquisitionWorkflow,
    finalize_captured_hunnu_springer_acceptance,
)

from literature_test_support import write_minimal_pdf, write_minimal_pdf_with_text


FIXTURES = Path(__file__).parent / "fixtures" / "literature"
TARGET_TITLE = (
    "Accounting for the middle: motivations, extent, and limitations of middle managers’ earnings management"
)
TARGET_DOI = "10.1007/s11573-023-01162-8"
TARGET_ARTICLE = f"https://link.springer.com/article/{TARGET_DOI}"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def route_result(*, resolved: bool = True, matched: bool | str = True) -> InstitutionalRouteResult:
    return InstitutionalRouteResult(
        requested_source="SpringerLink",
        resolution_trigger=InstitutionalResolutionTrigger.ACCESS_ROUTE_UNKNOWN,
        institutional_route_resolved=resolved,
        institutional_target_database_match=matched,
        route_result="SUCCESS" if resolved else "SOURCE_LAYOUT_CHANGED",
        reason="fixture route",
        route_steps=(
            InstitutionalRouteStep(
                1,
                "HUNNU Official Portal",
                "湖南师范大学",
                "www.hunnu.edu.cn",
                "https://www.hunnu.edu.cn/",
                "official portal reached",
                "2026-08-15T00:00:00+00:00",
            ),
            InstitutionalRouteStep(
                2,
                "SpringerLink / Publisher",
                "Springer Nature Link",
                "link.springer.com",
                "https://link.springer.com/",
                "official publisher reached through HUNNU route",
                "2026-08-15T00:00:01+00:00",
            ),
        ),
        publisher_entry_url=(
            "https://yclib.hunnu.edu.cn/vpn/983/https/OPAQUE-PUBLISHER-ROUTE/"
            "?temporary-navigation=value"
        ),
        generated_at="2026-08-15T00:00:02+00:00",
    )


class _MockPage:
    def __init__(self, browser: "_MockBrowser") -> None:
        self.browser = browser

    @property
    def url(self) -> str:
        return self.browser.current_url

    async def content(self) -> str:
        return self.browser.current_html

    async def title(self) -> str:
        parser = HUNNUInstitutionalAccessResolver._parse(self.browser.current_html)
        return parser.title


class _MockBrowser:
    def __init__(self, pages: dict[str, str | tuple[str, str]]) -> None:
        self.pages = pages
        self.current_url = "about:blank"
        self.current_html = "<html><title>blank</title></html>"
        self.history: list[str] = []
        self.page = _MockPage(self)
        self.downloads_dir = TEMP_DIR / "test-browser"

    async def goto(self, url: str) -> None:
        self.history.append(url)
        if url not in self.pages:
            raise AssertionError(f"Unexpected fixture navigation: {url}")
        value = self.pages[url]
        if isinstance(value, tuple):
            self.current_html, self.current_url = value
        else:
            self.current_html, self.current_url = value, url


def successful_route_pages(*, article_html: str | None = None) -> dict[str, str | tuple[str, str]]:
    pages: dict[str, str | tuple[str, str]] = {
        "https://www.hunnu.edu.cn/": fixture("hunnu_portal.html"),
        "https://lib.hunnu.edu.cn/": fixture("hunnu_library.html"),
        "https://lib.hunnu.edu.cn/resource/foreign": fixture("hunnu_foreign_databases.html"),
        "https://lib.hunnu.edu.cn/database/springer": fixture("hunnu_springer_detail.html"),
        "https://link.springer.com/": fixture("springer_publisher_home.html"),
    }
    if article_html is not None:
        pages[TARGET_ARTICLE] = article_html
    return pages


def target_record() -> LiteratureRecord:
    return LiteratureRecord(
        paper_id="EXPECTED",
        title=TARGET_TITLE,
        authors=("Sebastian Wagener",),
        year="2024",
        journal="Journal of Business Economics",
        doi=TARGET_DOI,
        source_database="SpringerLink",
        source_page=TARGET_ARTICLE,
        stable_identifier=TARGET_DOI,
        search_query=TARGET_TITLE,
    )


def target_request() -> LiteratureSearchRequest:
    return LiteratureSearchRequest.from_mapping(
        {
            "OriginalResearchRequest": f"Find exact Springer paper: {TARGET_TITLE}",
            "ExactTitles": [TARGET_TITLE],
            "DOIs": [TARGET_DOI],
            "KeywordsEN": ["earnings management"],
            "MaxSearchResults": 1,
            "MaxResultsPerSource": 1,
            "MaxDownloads": 1,
            "MaxDownloadsPerRun": 1,
            "RequireFullText": True,
        }
    )


class InstitutionalResolverParsingTests(unittest.TestCase):
    def test_hunnu_domain_accepts_official_subdomains_and_rejects_lookalikes(self):
        self.assertTrue(HUNNUInstitutionalAccessResolver.is_official_hunnu_url("https://lib.hunnu.edu.cn/"))
        self.assertTrue(HUNNUInstitutionalAccessResolver.is_official_hunnu_url("https://www.hunnu.edu.cn/"))
        self.assertFalse(HUNNUInstitutionalAccessResolver.is_official_hunnu_url("https://hunnu.edu.cn.example.test/"))

    def test_trusted_library_service_domain_is_exactly_allowlisted(self):
        self.assertTrue(
            HUNNUInstitutionalAccessResolver.is_trusted_hunnu_route_url(
                "https://wisdom.chaoxing.com/newwisdom/database"
            )
        )
        self.assertFalse(
            HUNNUInstitutionalAccessResolver.is_trusted_hunnu_route_url(
                "https://wisdom.chaoxing.com.example.test/newwisdom/database"
            )
        )

    def test_hunnu_gateway_requires_matching_rendered_publisher_identity(self):
        gateway = "https://yclib.hunnu.edu.cn/vpn/983/https/OPAQUE-PUBLISHER-ROUTE/"
        self.assertTrue(
            HUNNUInstitutionalAccessResolver.is_verified_source_destination(
                "SpringerLink", gateway, page_title="Home | Springer Nature Link"
            )
        )
        self.assertFalse(
            HUNNUInstitutionalAccessResolver.is_verified_source_destination(
                "SpringerLink", gateway, page_title="Unrelated database"
            )
        )
        self.assertEqual(
            HUNNUInstitutionalAccessResolver.provenance_url(gateway),
            "https://yclib.hunnu.edu.cn/vpn/",
        )

    def test_multiple_distinct_gateway_destinations_remain_ambiguous_without_exposing_paths(self):
        html = (
            '<a href="https://yclib.hunnu.edu.cn/vpn/983/https/OPAQUE-A/">网络地址 A</a>'
            '<a href="https://yclib.hunnu.edu.cn/vpn/983/https/OPAQUE-B/">网络地址 B</a>'
        )
        entries = HUNNUInstitutionalAccessResolver.discover_gateway_entries(
            html, base_url="https://wisdom.chaoxing.com/detail"
        )
        self.assertEqual(len(entries), 2)
        self.assertEqual({item.stable_url for item in entries}, {"https://yclib.hunnu.edu.cn/vpn/"})
        chosen, matched = HUNNUInstitutionalAccessResolver.choose_candidate(entries)
        self.assertIsNone(chosen)
        self.assertEqual(matched, "uncertain")

    def test_hunnu_library_entry_is_discovered_and_external_lookalike_is_ignored(self):
        entries = HUNNUInstitutionalAccessResolver.discover_library_entries(
            fixture("hunnu_portal.html"), base_url="https://www.hunnu.edu.cn/"
        )
        self.assertEqual([item.stable_url for item in entries], ["https://lib.hunnu.edu.cn/"])

    def test_visible_electronic_resource_entry_is_discovered(self):
        entries = HUNNUInstitutionalAccessResolver.discover_resource_entries(
            fixture("hunnu_library.html"), base_url="https://lib.hunnu.edu.cn/"
        )
        self.assertEqual(entries[0].stable_url, "https://lib.hunnu.edu.cn/resource/foreign")

    def test_springerlink_alias_matches(self):
        entries = HUNNUInstitutionalAccessResolver.discover_database_entries(
            fixture("hunnu_springer_direct.html"),
            base_url="https://lib.hunnu.edu.cn/resources",
            requested_source="SpringerLink",
        )
        self.assertEqual(entries[0].domain, "link.springer.com")

    def test_springer_nature_link_alias_matches(self):
        entries = HUNNUInstitutionalAccessResolver.discover_database_entries(
            fixture("hunnu_springer_alias_nature.html"),
            base_url="https://lib.hunnu.edu.cn/resources",
            requested_source="Springer Nature Link",
        )
        self.assertEqual(entries[0].domain, "link.springer.com")

    def test_springer_chinese_alias_matches_hunnu_detail(self):
        entries = HUNNUInstitutionalAccessResolver.discover_database_entries(
            fixture("hunnu_foreign_databases.html"),
            base_url="https://lib.hunnu.edu.cn/resource/foreign",
            requested_source="Springer电子期刊",
        )
        self.assertEqual(entries[0].stable_url, "https://lib.hunnu.edu.cn/database/springer")

    def test_official_publisher_domain_alone_is_strong_target_evidence(self):
        html = '<a href="https://link.springer.com/">网络地址</a>'
        entries = HUNNUInstitutionalAccessResolver.discover_database_entries(
            html, base_url="https://lib.hunnu.edu.cn/db/springer", requested_source="SpringerLink"
        )
        self.assertEqual(entries[0].match_basis, "official-domain")

    def test_duplicate_links_to_same_stable_destination_are_collapsed(self):
        html = (
            '<a href="https://link.springer.com/?from=one">SpringerLink</a>'
            '<a href="https://link.springer.com/?from=two">Springer Link</a>'
        )
        entries = HUNNUInstitutionalAccessResolver.discover_database_entries(
            html, base_url="https://lib.hunnu.edu.cn/", requested_source="SpringerLink"
        )
        self.assertEqual(len(entries), 1)
        chosen, matched = HUNNUInstitutionalAccessResolver.choose_candidate(entries)
        self.assertTrue(matched)
        self.assertIsNotNone(chosen)

    def test_equally_ranked_distinct_candidates_are_rejected_as_ambiguous(self):
        entries = HUNNUInstitutionalAccessResolver.discover_database_entries(
            fixture("hunnu_springer_ambiguous.html"),
            base_url="https://lib.hunnu.edu.cn/resources",
            requested_source="SpringerLink",
        )
        chosen, matched = HUNNUInstitutionalAccessResolver.choose_candidate(entries)
        self.assertIsNone(chosen)
        self.assertEqual(matched, "uncertain")

    def test_unrelated_database_is_not_treated_as_requested_target(self):
        html = '<a href="https://example.test/database">Other Publisher</a>'
        entries = HUNNUInstitutionalAccessResolver.discover_database_entries(
            html, base_url="https://lib.hunnu.edu.cn/", requested_source="SpringerLink"
        )
        self.assertEqual(entries, [])

    def test_known_negative_authorization_is_not_a_resolver_trigger(self):
        values = {item.value for item in InstitutionalResolutionTrigger}
        self.assertNotIn("FullTextAccessible=false", values)
        self.assertEqual(len(values), 4)

    def test_route_resolution_does_not_imply_fulltext_access(self):
        route = route_result()
        payload = route.as_dict()
        self.assertTrue(payload["InstitutionalRouteResolved"])
        self.assertFalse(payload["FullTextAccessRechecked"])
        self.assertEqual(payload["FullTextAccessible"], "unknown")

    def test_explicit_unauthorized_recheck_remains_unauthorized(self):
        access = AccessDecision(
            False,
            AccessType.METADATA_ONLY,
            False,
            RunStatus.FULLTEXT_NOT_AUTHORIZED,
            "No enabled official PDF control",
        )
        rechecked = route_result().with_access_decision(access)
        self.assertTrue(rechecked.full_text_access_rechecked)
        self.assertFalse(rechecked.full_text_accessible)

    def test_harmless_login_navigation_label_does_not_trigger_auth_gate(self):
        html = '<html><body><a href="/login">统一认证登录</a><main>数据库列表</main></body></html>'
        HUNNUInstitutionalAccessResolver.detect_manual_authentication(
            html, url="https://lib.hunnu.edu.cn/resources"
        )

    def test_password_form_requires_manual_authentication(self):
        with self.assertRaises(SourceActionRequired) as context:
            HUNNUInstitutionalAccessResolver.detect_manual_authentication(
                fixture("hunnu_manual_login.html"),
                url="https://authserver.hunnu.edu.cn/authserver/login",
            )
        self.assertIn("ACTION_REQUIRED_USER_LOGIN=true", str(context.exception))
        self.assertIn("BrowserReadyForManualAction=true", str(context.exception))

    def test_captcha_requires_manual_action_and_is_not_bypassed(self):
        with self.assertRaises(SourceActionRequired) as context:
            HUNNUInstitutionalAccessResolver.detect_manual_authentication(
                fixture("hunnu_captcha.html"), url="https://lib.hunnu.edu.cn/verify"
            )
        self.assertIn("CAPTCHA", str(context.exception))


class InstitutionalResolverNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_retries_one_unavailable_observation_then_succeeds(self):
        class _SettlingPort:
            downloads_dir = TEMP_DIR
            session = SessionHandle()
            page_handle = PageHandle(session=session)

            def __init__(self) -> None:
                self.observations = 0

            async def execute(self, command):
                if isinstance(command, NavigateCommand):
                    return BrowserActionResult(self.session, self.page_handle, 0, "navigate", command.url)
                self.assertIsInstance(command, ObserveCommand)
                self.observations += 1
                if self.observations == 1:
                    raise ObservationUnavailable("page is navigating")
                return BrowserObservation(
                    self.session,
                    self.page_handle,
                    0,
                    "https://www.hunnu.edu.cn/",
                    html="<html><title>HUNNU</title></html>",
                )

            def assertIsInstance(self, value, expected) -> None:
                if not isinstance(value, expected):
                    raise AssertionError(f"expected {expected}, got {type(value)}")

        port = _SettlingPort()
        with patch.object(institutional_module, "_SNAPSHOT_SETTLE_DELAY_SECONDS", 0):
            snapshot = await HUNNUInstitutionalAccessResolver(port)._snapshot_after_goto(
                "https://www.hunnu.edu.cn/"
            )
        self.assertEqual(snapshot, ("<html><title>HUNNU</title></html>", "https://www.hunnu.edu.cn/", "HUNNU"))
        self.assertEqual(port.observations, 2)

    async def test_snapshot_reraises_after_the_bounded_observation_attempts(self):
        class _UnavailablePort:
            downloads_dir = TEMP_DIR
            session = SessionHandle()
            page_handle = PageHandle(session=session)

            def __init__(self) -> None:
                self.observations = 0
                self.failure = ObservationUnavailable("page is still navigating")

            async def execute(self, command):
                if isinstance(command, NavigateCommand):
                    return BrowserActionResult(self.session, self.page_handle, 0, "navigate", command.url)
                if isinstance(command, ObserveCommand):
                    self.observations += 1
                    raise self.failure
                raise AssertionError(f"unexpected command: {command}")

        port = _UnavailablePort()
        with patch.object(institutional_module, "_SNAPSHOT_SETTLE_DELAY_SECONDS", 0):
            with self.assertRaisesRegex(ObservationUnavailable, "still navigating") as caught:
                await HUNNUInstitutionalAccessResolver(port)._snapshot_after_goto(
                    "https://www.hunnu.edu.cn/"
                )
        self.assertIs(caught.exception, port.failure)
        self.assertEqual(port.observations, institutional_module._SNAPSHOT_SETTLE_MAX_ATTEMPTS)

    async def test_successful_route_records_official_steps_without_access_assumption(self):
        browser = _MockBrowser(successful_route_pages())
        resolver = HUNNUInstitutionalAccessResolver(browser)
        route = await resolver.resolve(
            "Springer Nature Link",
            trigger=InstitutionalResolutionTrigger.ACCESS_ROUTE_UNKNOWN,
        )
        self.assertTrue(route.institutional_route_resolved)
        self.assertIs(route.institutional_target_database_match, True)
        self.assertFalse(route.full_text_access_rechecked)
        self.assertIsNone(route.full_text_accessible)
        self.assertEqual(len(route.route_steps), 5)
        self.assertEqual(route.route_steps[-1].official_domain, "link.springer.com")

    async def test_realistic_hunnu_hosted_detail_and_gateway_route_is_locked(self):
        hosted_detail = "https://wisdom.chaoxing.com/newwisdom/doordatabase/databasedetail.html?id=31660"
        gateway = "https://yclib.hunnu.edu.cn/vpn/983/https/OPAQUE-PUBLISHER-ROUTE/"
        pages = {
            "https://www.hunnu.edu.cn/": fixture("hunnu_portal.html"),
            "https://lib.hunnu.edu.cn/": fixture("hunnu_library.html"),
            "https://lib.hunnu.edu.cn/resource/foreign": (
                f'<html><title>外文数据库</title><a href="{hosted_detail}">Springer LINK全文期刊</a></html>'
            ),
            hosted_detail: fixture("hunnu_springer_detail_gateway.html"),
            gateway: fixture("springer_gateway_home.html"),
        }
        route = await HUNNUInstitutionalAccessResolver(_MockBrowser(pages)).resolve(
            "SpringerLink",
            trigger=InstitutionalResolutionTrigger.ACCESS_ROUTE_UNKNOWN,
        )
        self.assertTrue(route.institutional_route_resolved)
        self.assertTrue(route.institutional_target_database_match)
        self.assertEqual(route.route_steps[-1].official_domain, "yclib.hunnu.edu.cn")
        self.assertEqual(route.route_steps[-1].stable_url, "https://yclib.hunnu.edu.cn/vpn/")

    async def test_ambiguous_target_stops_before_any_publisher_navigation(self):
        pages = successful_route_pages()
        pages["https://lib.hunnu.edu.cn/resource/foreign"] = fixture("hunnu_springer_ambiguous.html")
        browser = _MockBrowser(pages)
        route = await HUNNUInstitutionalAccessResolver(browser).resolve(
            "SpringerLink",
            trigger=InstitutionalResolutionTrigger.DIRECT_ROUTE_UNAVAILABLE,
        )
        self.assertFalse(route.institutional_route_resolved)
        self.assertEqual(route.institutional_target_database_match, "uncertain")
        self.assertNotIn("https://link.springer.com/", browser.history)

    async def test_manual_login_redirect_stops_without_authentication_actions(self):
        browser = _MockBrowser(
            {
                "https://www.hunnu.edu.cn/": (
                    fixture("hunnu_manual_login.html"),
                    "https://authserver.hunnu.edu.cn/authserver/login",
                )
            }
        )
        with self.assertRaises(SourceActionRequired):
            await HUNNUInstitutionalAccessResolver(browser).resolve(
                "SpringerLink",
                trigger=InstitutionalResolutionTrigger.INSTITUTIONAL_LOGIN_ENTRY_NOT_FOUND,
            )
        self.assertEqual(browser.history, ["https://www.hunnu.edu.cn/"])

    async def test_resolve_and_recheck_confirms_target_then_reads_authorization(self):
        article_html = fixture("springerlink_article_open_access.html")
        browser = _MockBrowser(successful_route_pages(article_html=article_html))
        resolver = HUNNUInstitutionalAccessResolver(browser)
        record = target_record()
        route, access = await resolver.resolve_and_recheck(
            SpringerLinkAdapter(browser),
            record,
            trigger=InstitutionalResolutionTrigger.FULL_TEXT_STATUS_UNKNOWN,
        )
        self.assertTrue(route.full_text_access_rechecked)
        self.assertTrue(route.full_text_accessible)
        self.assertTrue(access.authorized_access)
        self.assertTrue(record.target_identity_confirmed)

    async def test_resolve_and_recheck_can_return_known_unauthorized(self):
        article_html = fixture("springerlink_article_open_access.html").replace(
            '<a href="/content/pdf/10.1007/s11573-023-01162-8.pdf">Download PDF</a>',
            "<span>Download unavailable</span>",
        )
        browser = _MockBrowser(successful_route_pages(article_html=article_html))
        route, access = await HUNNUInstitutionalAccessResolver(browser).resolve_and_recheck(
            SpringerLinkAdapter(browser),
            target_record(),
            trigger=InstitutionalResolutionTrigger.FULL_TEXT_STATUS_UNKNOWN,
        )
        self.assertTrue(route.institutional_route_resolved)
        self.assertTrue(route.full_text_access_rechecked)
        self.assertFalse(route.full_text_accessible)
        self.assertFalse(access.authorized_access)

    async def test_target_identity_change_after_route_is_rejected(self):
        browser = _MockBrowser(successful_route_pages(article_html=fixture("springerlink_article_open_access.html")))
        record = target_record()
        record.doi = "10.1007/not-the-target"
        record.source_page = TARGET_ARTICLE
        with self.assertRaises(SourceLayoutChanged):
            await HUNNUInstitutionalAccessResolver(browser).resolve_and_recheck(
                SpringerLinkAdapter(browser),
                record,
                trigger=InstitutionalResolutionTrigger.ACCESS_ROUTE_UNKNOWN,
            )
        self.assertFalse(record.target_identity_confirmed)


class InstitutionalArtifactAndWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_known_false_access_without_explicit_trigger_does_not_call_resolver(self):
        class Adapter(SpringerLinkAdapter):
            human_like_delay_seconds = 0

            async def search(self, query, request):
                return [target_record()]

            async def open_result(self, record):
                return None

            async def extract_metadata(self, *, search_query):
                record = target_record()
                record.abstract = "Earnings management evidence."
                record.publication_status = PublicationStatus.PEER_REVIEWED_JOURNAL_ARTICLE.value
                return record

            async def check_fulltext_access(self):
                return AccessDecision(
                    False,
                    AccessType.METADATA_ONLY,
                    False,
                    RunStatus.FULLTEXT_NOT_AUTHORIZED,
                    "Known unauthorized fixture state",
                )

        class Resolver:
            def __init__(self):
                self.calls = 0

            async def resolve_and_recheck(self, *args, **kwargs):
                self.calls += 1
                raise AssertionError("resolver must not be called without an explicit trigger")

        resolver = Resolver()
        with tempfile.TemporaryDirectory() as tmp:
            workflow = LiteratureAcquisitionWorkflow(
                Adapter(browser=None),
                run_root=Path(tmp) / "run",
                human_like_delay_seconds=0,
                allow_outside_project_for_tests=True,
                institutional_resolver=resolver,
                institutional_trigger=None,
            )
            result = await workflow.run(target_request())
        self.assertEqual(resolver.calls, 0)
        self.assertEqual(result.status, RunStatus.FULLTEXT_NOT_AUTHORIZED)

    async def test_explicit_trigger_runs_resolver_and_writes_provenance(self):
        class Adapter(SpringerLinkAdapter):
            human_like_delay_seconds = 0

            def __init__(self, root: Path):
                super().__init__(browser=None)
                self.root = root

            async def search(self, query, request):
                return [target_record()]

            async def open_result(self, record):
                return None

            async def extract_metadata(self, *, search_query):
                record = target_record()
                record.abstract = "Earnings management evidence."
                record.publication_status = PublicationStatus.PEER_REVIEWED_JOURNAL_ARTICLE.value
                return record

            async def check_fulltext_access(self):
                return AccessDecision(
                    False,
                    AccessType.METADATA_ONLY,
                    False,
                    RunStatus.FULLTEXT_NOT_AUTHORIZED,
                    "Direct route unknown fixture state",
                )

            async def download_fulltext(self, record, access):
                return write_minimal_pdf(self.root / "source.pdf")

        class Resolver:
            def __init__(self):
                self.calls = 0

            async def resolve_and_recheck(self, adapter, record, *, trigger):
                self.calls += 1
                record.target_identity_confirmed = True
                access = AccessDecision(
                    True,
                    AccessType.INSTITUTIONAL_AUTHENTICATED,
                    True,
                    RunStatus.SUCCESS,
                    "Official PDF control after explicit institutional route",
                )
                return route_result().with_access_decision(access), access

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            resolver = Resolver()
            workflow = LiteratureAcquisitionWorkflow(
                Adapter(root),
                run_root=root / "run",
                human_like_delay_seconds=0,
                allow_outside_project_for_tests=True,
                institutional_resolver=resolver,
                institutional_trigger=InstitutionalResolutionTrigger.ACCESS_ROUTE_UNKNOWN,
            )
            result = await workflow.run(target_request())
            provenance = json.loads(
                (root / "run" / "manifests" / "INSTITUTIONAL_ROUTE_PROVENANCE.json").read_text(
                    encoding="utf-8"
                )
            )
            manifest = json.loads((root / "run" / "DOWNLOAD_MANIFEST.json").read_text(encoding="utf-8"))
        self.assertEqual(resolver.calls, 1)
        self.assertEqual(result.status, RunStatus.SUCCESS)
        self.assertEqual(len(result.downloads), 1)
        self.assertTrue(provenance["Routes"][0]["FullTextAccessRechecked"])
        self.assertTrue(manifest["InstitutionalRoutes"][0]["InstitutionalRouteResolved"])

    async def test_route_provenance_uses_stable_urls_and_contains_no_navigation_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            writer = LiteratureArtifactWriter(Path(tmp), allow_outside_project_for_tests=True)
            writer.write_institutional_route_provenance((route_result(),))
            content = writer.institutional_route_provenance_path.read_text(encoding="utf-8")
            self.assertNotIn("temporary-navigation", content)
            self.assertEqual(scan_text_for_sensitive_leaks(content), [])


class InstitutionalSecurityTests(unittest.TestCase):
    def test_saml_and_oauth_credential_shapes_are_detected_but_policy_booleans_are_safe(self):
        findings = scan_text_for_sensitive_leaks(
            "saml_assertion=encoded-value-12345\noauth_credential=credential-value-67890\n"
        )
        self.assertEqual({finding.term for finding in findings}, {"saml_assertion", "oauth_credential"})
        self.assertEqual(
            scan_text_for_sensitive_leaks("`SAML=false`\nOAuth=false\nTokensExported=false\n"),
            [],
        )


class InstitutionalSpringerFinalizerTests(unittest.TestCase):
    def _cleanup_read_only(self, root: Path) -> None:
        for path in root.rglob("*"):
            if path.is_file():
                path.chmod(path.stat().st_mode | stat.S_IWRITE)

    def test_finalizer_rejects_wrong_target_before_any_archive_write(self):
        temp_parent = TEMP_DIR
        temp_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_parent) as tmp:
            root = Path(tmp)
            source = write_minimal_pdf_with_text(
                root / "captured.pdf",
                f"Sebastian Wagener DOI: {TARGET_DOI}",
            )
            run_root = root / "run"
            with self.assertRaises(SourceLayoutChanged):
                finalize_captured_hunnu_springer_acceptance(
                    request=target_request(),
                    query=TARGET_TITLE,
                    search_html=fixture("springerlink_search.html"),
                    article_html=fixture("springerlink_article_open_access.html"),
                    article_url=TARGET_ARTICLE,
                    downloaded_pdf=source,
                    institutional_route=route_result(),
                    expected_title="A different paper",
                    expected_doi=TARGET_DOI,
                    run_root=run_root,
                )
            self.assertFalse((run_root / "downloads" / "archive").exists())

    def test_finalizer_locks_identity_validates_pdf_hashes_and_writes_route_manifest(self):
        temp_parent = TEMP_DIR
        temp_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_parent) as tmp:
            root = Path(tmp)
            source = write_minimal_pdf_with_text(
                root / "captured.pdf",
                f"Sebastian Wagener DOI: {TARGET_DOI}",
            )
            run_root = root / "run"
            result = finalize_captured_hunnu_springer_acceptance(
                request=target_request(),
                query=TARGET_TITLE,
                search_html=fixture("springerlink_search.html"),
                article_html=fixture("springerlink_article_open_access.html"),
                article_url=TARGET_ARTICLE,
                downloaded_pdf=source,
                institutional_route=route_result(),
                expected_title=TARGET_TITLE,
                expected_doi=TARGET_DOI,
                run_root=run_root,
            )
            manifest = json.loads((run_root / "DOWNLOAD_MANIFEST.json").read_text(encoding="utf-8"))
            provenance = json.loads(
                (run_root / "manifests" / "INSTITUTIONAL_ROUTE_PROVENANCE.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(result.status, RunStatus.SUCCESS)
            self.assertTrue(result.records[0].target_identity_confirmed)
            self.assertEqual(len(result.downloads[0].sha256), 64)
            self.assertTrue(result.downloads[0].file_validation_passed)
            self.assertTrue(manifest["Downloads"][0]["TargetIdentityConfirmed"])
            self.assertTrue(provenance["Routes"][0]["FullTextAccessRechecked"])
            self.assertEqual(scan_text_for_sensitive_leaks(json.dumps(manifest)), [])
            self._cleanup_read_only(root)


class OxfordDetailPageRouteTests(unittest.IsolatedAsyncioTestCase):
    """The HUNNU detail page publishes its address in a click handler.

    Observed live on 2026-09-19: the Oxford route stopped with "HUNNU database
    detail did not expose one unambiguous official publisher link" because
    _RouteHTMLParser collected only <a> elements, and this page has no anchor
    carrying the publisher address at all.  The resolver was then driven with
    HUNNU_OXFORD_ROUTE_URL as a manual workaround; these tests pin that the
    route no longer needs it.
    """

    DETAIL_URL = (
        "https://wisdom.chaoxing.com/newwisdom/doordatabase/"
        "databasedetail.html?wfwfid=125449&pageId=36761&id=26605"
    )
    OXFORD_ENTRY = "https://academic.oup.com/journals"

    def test_the_publisher_address_in_a_click_handler_is_read(self):
        entries = HUNNUInstitutionalAccessResolver.discover_database_entries(
            fixture("hunnu_oxford_detail.html"),
            base_url=self.DETAIL_URL,
            requested_source="OxfordAcademic",
        )
        urls = {entry.navigation_url for entry in entries}
        self.assertIn(self.OXFORD_ENTRY, urls)

    def test_the_off_campus_carsi_entry_is_not_an_unattended_candidate(self):
        """This failing means the run may be steered into a federated sign-in.

        It is also what keeps the two published addresses from tying under
        choose_candidate and failing closed on an ambiguity that is not real.
        """

        entries = HUNNUInstitutionalAccessResolver.discover_database_entries(
            fixture("hunnu_oxford_detail.html"),
            base_url=self.DETAIL_URL,
            requested_source="OxfordAcademic",
        )
        self.assertNotIn("https://academic.oup.com/", {e.navigation_url for e in entries})
        chosen, match = HUNNUInstitutionalAccessResolver.choose_candidate(entries)
        self.assertIs(match, True)
        self.assertEqual(chosen.navigation_url, self.OXFORD_ENTRY)

    def test_the_handler_is_read_never_evaluated(self):
        """Only a percent-encoded absolute http(s) argument is a candidate."""

        hostile = (
            "<html><body>"
            "<span onclick=\"redirecturl(1,0,1,'','javascript%3Aalert(1)',0)\">a</span>"
            "<span onclick=\"redirecturl(2,0,1,'','%2Frelative%2Fpath',0)\">b</span>"
            "<span onclick=\"redirecturl(3,0,1,'','https%3A%2F%2Fuser%3Apw%40evil.example%2F',0)\">c</span>"
            "<span onclick=\"otherhandler('https%3A%2F%2Facademic.oup.com%2F')\">d</span>"
            "<span onclick=\"redirecturl(5,0,1,'https%3A%2F%2Fa.example%2F','https%3A%2F%2Fb.example%2F',0)\">e</span>"
            "</body></html>"
        )
        parser = HUNNUInstitutionalAccessResolver._parse(hostile)
        self.assertEqual(parser.script_links, [])

    def test_an_ordinary_page_gains_no_candidates_from_this(self):
        """A page whose links are real anchors must behave exactly as before."""

        parser = HUNNUInstitutionalAccessResolver._parse(
            fixture("hunnu_springer_detail.html")
        )
        self.assertEqual(parser.script_links, [])

    async def test_the_whole_route_resolves_without_the_environment_override(self):
        pages = {
            "https://www.hunnu.edu.cn/": fixture("hunnu_portal.html"),
            "https://lib.hunnu.edu.cn/": fixture("hunnu_library.html"),
            self.DETAIL_URL: fixture("hunnu_oxford_detail.html"),
            self.OXFORD_ENTRY: (
                "<html><head><title>Journals | Oxford Academic</title></head>"
                "<body>Oxford Academic</body></html>"
            ),
        }
        browser = _MockBrowser(pages)
        # The detail URL is supplied as the discovered entry, not as the env
        # override: what is under test is reading the page, not the override.
        resolver = HUNNUInstitutionalAccessResolver(browser, oxford_route_url=self.DETAIL_URL)
        result = await resolver.resolve(
            "OxfordAcademic",
            trigger=InstitutionalResolutionTrigger.DIRECT_ROUTE_UNAVAILABLE,
        )
        self.assertTrue(result.institutional_route_resolved)
        self.assertIs(result.institutional_target_database_match, True)
        self.assertEqual(result.publisher_entry_url, self.OXFORD_ENTRY)

    def test_the_real_navigation_page_still_yields_one_oxford_entry(self):
        """The click handlers must not create a rival that outranks the entry."""

        entries = HUNNUInstitutionalAccessResolver.discover_database_entries(
            fixture("hunnu_oxford_navigation.html"),
            base_url="https://wisdom.chaoxing.com/newwisdom/doordatabase/database.html",
            requested_source="OxfordAcademic",
        )
        chosen, match = HUNNUInstitutionalAccessResolver.choose_candidate(entries)
        self.assertIs(match, True)
        self.assertIn("id=26605", chosen.navigation_url)


if __name__ == "__main__":
    unittest.main()
