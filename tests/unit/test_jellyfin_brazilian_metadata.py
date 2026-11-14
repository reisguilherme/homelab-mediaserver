import copy
import json

import httpx
import pytest

POSTER = "https://image.tmdb.org/t/p/original/br-poster.jpg"
LOCAL_NAME = "Filme Brasileiro: O Retorno"


class BrazilianFixture:
    def __init__(self):
        self.users = [{"Id": "owner-id", "Name": "owner"}]
        self.items = [{
            "Id": "national", "Type": "Movie", "Name": "Brazilian Film: The Return",
            "OriginalTitle": "Filme Brasileiro", "SortName": "brazilian film the return",
            "ForcedSortName": "owner-custom-sort", "LockData": True,
            "LockedFields": ["Overview", "Genres"], "Path": "/media/national/movie.mkv",
            "ProviderIds": {"Tmdb": "123", "Imdb": "tt123", "Other": "keep"},
            "ProductionLocations": ["Brazil"], "ImageTags": {"Primary": "english-poster"},
            "PreferredMetadataLanguage": None, "PreferredMetadataCountryCode": None,
            "Overview": "owner text", "Genres": ["Drama"], "Tags": ["owner-tag"],
            "People": [{"Name": "Actor", "Type": "Actor", "Role": "Hero"}],
            "ProductionYear": 2020, "PremiereDate": "2020-01-01T00:00:00Z",
            "Studios": [{"Name": "Studio"}], "CommunityRating": 7.0,
            "OfficialRating": "12", "MediaSources": [{"Id": "source", "Path": "same"}],
        }]
        self.results = [{"ProviderIds": {"Tmdb": "123"}, "Name": LOCAL_NAME, "ImageUrl": POSTER}]
        self.images = [{"Url": POSTER, "Language": "pt", "ProviderName": "TheMovieDb",
                        "Type": "Primary", "Width": 2000}]
        self.requests = []
        self.reads = 0
        self.on_detail = None
        self.accept_update = True
        self.accept_download = True
        self.fail_item = None
        self.page_size = None
        self.downloads = 0
        self.same_image_tag = False
        self.image_available = True

    def __call__(self, request):
        self.requests.append(request)
        assert 'Token="private-fixture-key"' in request.headers["Authorization"]
        assert "X-Emby-Token" not in request.headers
        path = request.url.path
        if path == "/Users":
            return httpx.Response(200, json=self.users)
        if path == "/Items":
            assert request.url.params["IncludeItemTypes"] == "Movie"
            assert request.url.params["CollapseBoxSetItems"] == "false"
            assert request.url.params["UserId"] == "owner-id"
            assert "ProductionLocations" in request.url.params["Fields"]
            start = int(request.url.params["StartIndex"])
            size = self.page_size or int(request.url.params["Limit"])
            return httpx.Response(200, json={
                "Items": self.items[start:start + size], "TotalRecordCount": len(self.items),
            })
        if path.startswith("/Users/owner-id/Items/"):
            item_id = path.rsplit("/", 1)[1]
            if item_id == self.fail_item:
                return httpx.Response(503)
            self.reads += 1
            item = next(item for item in self.items if item["Id"] == item_id)
            if self.on_detail:
                self.on_detail(self.reads, item)
            return httpx.Response(200, json=item)
        if path == "/Items/RemoteSearch/Movie":
            assert request.method == "POST"
            body = json.loads(request.content)
            assert body["SearchInfo"]["ProviderIds"] == {"Tmdb": "123"}
            assert body["SearchInfo"]["MetadataLanguage"] == "pt-BR"
            assert body["SearchInfo"]["MetadataCountryCode"] == "BR"
            assert body["IncludeDisabledProviders"] is True
            return httpx.Response(200, json=self.results)
        item = next(item for item in self.items if path.startswith("/Items/" + item["Id"]))
        if path.endswith("/Images/Primary"):
            assert request.method == "GET"
            if not self.image_available:
                return httpx.Response(503)
            return httpx.Response(200, content=b"fixture-image-body",
                                  headers={"Content-Type": "image/jpeg"})
        if path.endswith("/RemoteImages"):
            assert request.method == "GET"
            assert request.url.params["type"] == "Primary"
            assert request.url.params["includeAllLanguages"] == "true"
            start = int(request.url.params.get("startIndex", "0"))
            limit = int(request.url.params.get("limit", "1000"))
            return httpx.Response(200, json={
                "Images": self.images[start:start + limit], "TotalRecordCount": len(self.images),
            })
        if path == "/Items/" + item["Id"]:
            assert request.method == "POST"
            if self.accept_update:
                item.update(json.loads(request.content))
            return httpx.Response(204)
        if path.endswith("/RemoteImages/Download"):
            assert request.method == "POST"
            assert request.url.params["type"] == "Primary"
            assert request.url.params["imageUrl"] == POSTER
            self.downloads += 1
            if self.accept_download and not self.same_image_tag:
                item["ImageTags"]["Primary"] = "portuguese-poster-" + str(self.downloads)
            return httpx.Response(204)
        raise AssertionError(f"Unexpected request {request.method} {path}")


def reconciler(client, **kwargs):
    from homeserver_control.worker.jellyfin_brazilian_metadata import (
        JellyfinBrazilianMetadataReconciler,
    )

    return JellyfinBrazilianMetadataReconciler(
        client, base_url="http://jellyfin:8096", api_key="private-fixture-key",
        admin_username="owner", interval_seconds=60, **kwargs,
    )


def writes(native):
    return [request for request in native.requests if request.method != "GET"
            and request.url.path != "/Items/RemoteSearch/Movie"]


@pytest.mark.asyncio
async def test_brazilian_exact_identity_localizes_name_and_portuguese_poster_preserving_full_dto():
    native = BrazilianFixture()
    before = copy.deepcopy(native.items[0])
    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
        report = await reconciler(client).reconcile()
    assert report["repaired"] == 1
    assert native.items[0] == {
        **before, "Name": LOCAL_NAME, "PreferredMetadataLanguage": "pt-BR",
        "PreferredMetadataCountryCode": "BR", "ImageTags": {"Primary": "portuguese-poster-1"},
    }
    payload = json.loads(writes(native)[0].content)
    assert payload == {
        **before, "Name": LOCAL_NAME, "PreferredMetadataLanguage": "pt-BR",
        "PreferredMetadataCountryCode": "BR",
    }
    assert [request.url.path for request in writes(native)] == [
        "/Items/national", "/Items/national/RemoteImages/Download",
    ]
    serialized = json.dumps(report)
    assert all(value not in serialized for value in
               ("private-fixture-key", POSTER, before["Path"], LOCAL_NAME, "tt123"))


@pytest.mark.asyncio
@pytest.mark.parametrize("countries", [["United States"], ["Portugal"], [], None, ["Not Brazil"]])
async def test_portuguese_title_or_metadata_language_does_not_classify_foreign_movies(countries):
    native = BrazilianFixture()
    native.items[0].update(ProductionLocations=countries, Name=LOCAL_NAME,
                           PreferredMetadataLanguage="pt-BR")
    before = copy.deepcopy(native.items)
    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
        report = await reconciler(client).reconcile()
    assert native.items == before
    assert writes(native) == []
    assert not any(request.url.path == "/Items/RemoteSearch/Movie" for request in native.requests)
    assert report["brazilian"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("countries", [["Brasil"], ["BR"], [" Brazil "], ["France", "Brazil"]])
async def test_exact_brazil_country_names_codes_and_coproductions_are_eligible(countries):
    native = BrazilianFixture()
    native.items[0]["ProductionLocations"] = countries
    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
        report = await reconciler(client).reconcile()
    assert report["repaired"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("results", [
    [], [{"ProviderIds": {"Tmdb": "456"}, "Name": LOCAL_NAME, "ImageUrl": POSTER}],
    [{"ProviderIds": {"Tmdb": "123", "tmdb": "123"}, "Name": LOCAL_NAME, "ImageUrl": POSTER}],
    [{"ProviderIds": {"Tmdb": "123"}, "Name": "", "ImageUrl": POSTER}],
    [{"ProviderIds": {"Tmdb": "123"}, "Name": LOCAL_NAME, "ImageUrl": POSTER}] * 2,
])
async def test_missing_ambiguous_identity_title_or_untrusted_image_never_writes(results):
    native = BrazilianFixture()
    native.results = results
    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
        report = await reconciler(client).reconcile()
    assert report["unresolved"] == 1
    assert writes(native) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("image_update", [
    {"Language": "en"}, {"Language": None}, {"Language": "pt-PT"},
    {"ProviderName": "Other"}, {"Url": "https://attacker.example/a.jpg"},
    {"Type": "Backdrop"}, {"Width": True}, {"Width": -1},
])
async def test_unavailable_portuguese_poster_does_not_block_exact_name_and_locale(image_update):
    native = BrazilianFixture()
    native.images[0].update(image_update)
    before = copy.deepcopy(native.items[0])
    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
        report = await reconciler(client).reconcile()
    assert report["unresolved"] == 1
    assert report["repaired"] == 0
    assert native.items[0] == {
        **before, "Name": LOCAL_NAME, "PreferredMetadataLanguage": "pt-BR",
        "PreferredMetadataCountryCode": "BR",
    }
    assert [request.url.path for request in writes(native)] == ["/Items/national"]
    assert native.downloads == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_images", [[], [{
    "Url": POSTER, "Language": "en", "ProviderName": "TheMovieDb",
    "Type": "Primary", "Width": 2000,
}]])
async def test_pending_poster_retries_without_metadata_rewrite_then_repairs_new_portuguese_art(
    initial_images,
):
    native = BrazilianFixture()
    native.images = initial_images
    before = copy.deepcopy(native.items[0])
    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
        worker = reconciler(client)
        assert (await worker.reconcile())["unresolved"] == 1
        assert native.items[0] == {
            **before, "Name": LOCAL_NAME, "PreferredMetadataLanguage": "pt-BR",
            "PreferredMetadataCountryCode": "BR",
        }
        assert [request.url.path for request in writes(native)] == ["/Items/national"]
        assert native.downloads == 0
        assert (await worker.reconcile(force=True))["unresolved"] == 1
        assert [request.url.path for request in writes(native)] == ["/Items/national"]
        assert native.downloads == 0
        native.images = [{
            "Url": POSTER, "Language": "pt", "ProviderName": "TheMovieDb",
            "Type": "Primary", "Width": 2000,
        }]
        assert (await worker.reconcile(force=True))["repaired"] == 1
        assert (await worker.reconcile(force=True))["preserved"] == 1
    assert native.downloads == 1
    assert [request.url.path for request in writes(native)] == [
        "/Items/national", "/Items/national/RemoteImages/Download",
    ]


@pytest.mark.asyncio
async def test_name_locale_without_poster_still_require_exact_metadata_readback():
    native = BrazilianFixture()
    native.images = []
    native.accept_update = False
    before = copy.deepcopy(native.items[0])
    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
        report = await reconciler(client).reconcile()
    assert report["failed"] == 1
    assert report["unresolved"] == 0
    assert native.items[0] == before
    assert native.downloads == 0


@pytest.mark.asyncio
async def test_existing_primary_is_localized_once_and_tag_change_requires_new_verification():
    native = BrazilianFixture()
    native.items[0].update(Name=LOCAL_NAME, PreferredMetadataLanguage="pt-BR",
                           PreferredMetadataCountryCode="BR")
    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
        worker = reconciler(client)
        assert (await worker.reconcile())["repaired"] == 1
        first_writes = len(writes(native))
        assert (await worker.reconcile(force=True))["preserved"] == 1
        assert len(writes(native)) == first_writes
        native.items[0]["ImageTags"]["Primary"] = "owner-new-image"
        assert (await worker.reconcile(force=True))["repaired"] == 1
    assert native.downloads == 2
    assert all(request.url.path.endswith("/RemoteImages/Download") for request in writes(native))


@pytest.mark.asyncio
async def test_fresh_dto_retains_concurrent_owner_edits_to_unrelated_fields():
    native = BrazilianFixture()

    def edit(read, item):
        if read == 2:
            item["Overview"] = "new owner text"
            item["LockedFields"] = ["Overview", "Genres", "CommunityRating"]
            item["ForcedSortName"] = "new owner sort"

    native.on_detail = edit
    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
        report = await reconciler(client).reconcile()
    assert report["repaired"] == 1
    assert native.items[0]["Overview"] == "new owner text"
    assert native.items[0]["LockedFields"] == ["Overview", "Genres", "CommunityRating"]
    assert native.items[0]["ForcedSortName"] == "new owner sort"


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("Path", "/different/movie.mkv"), ("ProviderIds", {"Tmdb": "456"}),
    ("ProductionLocations", ["Portugal"]), ("Type", "Episode"),
])
async def test_identity_or_nationality_drift_before_write_is_held(field, value):
    native = BrazilianFixture()

    def drift(read, item):
        if read == 2:
            item[field] = value

    native.on_detail = drift
    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
        report = await reconciler(client).reconcile()
    assert report["failed"] == 1
    assert writes(native) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("LockData", None), ("LockData", 1), ("Path", None), ("Name", None),
    ("ProviderIds", {"Tmdb": True}), ("ProviderIds", {"Tmdb": "123", "tmdb": "123"}),
    ("ImageTags", None), ("LockedFields", "Name"),
])
async def test_incomplete_native_movie_dto_cannot_clear_metadata_or_change_locks(field, value):
    native = BrazilianFixture()
    native.items[0][field] = value
    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
        report = await reconciler(client).reconcile()
    assert report["failed"] == 1
    assert writes(native) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "download"])
async def test_metadata_and_existing_image_acknowledgements_require_readback(operation):
    native = BrazilianFixture()
    setattr(native, "accept_" + operation, False)
    if operation == "download":
        native.image_available = False
    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
        report = await reconciler(client).reconcile()
    assert report["failed"] == 1
    assert report["repaired"] == 0
    if operation == "update":
        assert native.downloads == 0


@pytest.mark.asyncio
async def test_english_search_poster_uses_deterministic_best_portuguese_native_fallback():
    native = BrazilianFixture()
    english = "https://image.tmdb.org/t/p/original/english.jpg"
    native.results[0]["ImageUrl"] = english
    native.images = [
        {"Url": english, "Language": "en", "ProviderName": "TheMovieDb",
         "Type": "Primary", "Width": 4000, "Height": 6000},
        {"Url": "https://image.tmdb.org/t/p/original/small.jpg", "Language": "pt",
         "ProviderName": "TheMovieDb", "Type": "Primary", "Width": 500, "Height": 750},
        {"Url": POSTER, "Language": "pt-BR", "ProviderName": "TheMovieDb",
         "Type": "Primary", "Width": 2000, "Height": 3000},
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
        report = await reconciler(client).reconcile()
    assert report["repaired"] == 1
    assert native.items[0]["Name"] == LOCAL_NAME
    assert native.downloads == 1


@pytest.mark.asyncio
async def test_search_portuguese_url_is_preferred_over_higher_resolution_alternative():
    native = BrazilianFixture()
    native.images.append({
        "Url": "https://image.tmdb.org/t/p/original/larger.jpg", "Language": "pt",
        "ProviderName": "TheMovieDb", "Type": "Primary", "Width": 4000, "Height": 6000,
    })
    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
        report = await reconciler(client).reconcile()
    assert report["repaired"] == 1
    assert native.downloads == 1


@pytest.mark.asyncio
async def test_previously_proven_portuguese_tag_can_remain_identical_after_fresh_name_repair():
    native = BrazilianFixture()
    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
        worker = reconciler(client)
        assert (await worker.reconcile())["repaired"] == 1
        native.same_image_tag = True
        native.items[0]["Name"] = "A new untranslated name"
        assert (await worker.reconcile(force=True))["repaired"] == 1
        assert (await worker.reconcile(force=True))["preserved"] == 1
    assert native.downloads == 2
    assert native.items[0]["ImageTags"]["Primary"] == "portuguese-poster-1"
    assert any(request.url.path.endswith("/Images/Primary") for request in native.requests)


@pytest.mark.asyncio
async def test_ignored_download_cannot_prove_existing_english_primary_from_image_http_only():
    native = BrazilianFixture()
    native.accept_download = False
    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
        report = await reconciler(client).reconcile()
    assert report["failed"] == 1
    assert report["repaired"] == 0
    assert native.items[0]["ImageTags"]["Primary"] == "english-poster"


@pytest.mark.asyncio
async def test_interval_then_new_import_and_paginated_movies_are_reconciled_independently():
    native = BrazilianFixture()
    now = [0.0]
    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
        worker = reconciler(client, clock=lambda: now[0])
        await worker.reconcile()
        count = len(native.requests)
        now[0] = 59.0
        assert (await worker.reconcile())["status"] == "waiting"
        assert len(native.requests) == count
        native.items.append(dict(copy.deepcopy(native.items[0]), Id="new", ImageTags={}))
        native.page_size = 1
        native.fail_item = "national"
        now[0] = 60.0
        report = await worker.reconcile()
    assert report["movies"] == 2
    assert report["failed"] == 1
    assert report["repaired"] == 1
    assert native.items[1]["ImageTags"]["Primary"]


@pytest.mark.asyncio
async def test_ambiguous_owner_never_writes():
    native = BrazilianFixture()
    native.users.append({"Id": "other", "Name": "owner"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(native)) as client:
        report = await reconciler(client).reconcile()
    assert report["status"] == "unavailable"
    assert writes(native) == []
