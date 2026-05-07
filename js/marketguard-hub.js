(function () {
    const root = document.getElementById("marketguard-app");
    if (!root) {
        return;
    }

    const view = root.dataset.view || "lowestbin";
    const endpoint = root.dataset.apiUrl || "/api/v2/lowestbin";
    const basePath = root.dataset.basePath || "/market";
    const refreshIntervalMs = Math.max(1000, Number(root.dataset.refreshIntervalMs || "60000"));
    const grid = document.getElementById("market-grid");
    const sentinel = document.getElementById("market-sentinel");
    const alertSlot = document.getElementById("market-alert-slot");
    const emptyState = document.getElementById("market-empty-state");
    const emptyMessage = document.getElementById("market-empty-message");
    const refreshButton = document.getElementById("market-refresh-button");
    const refreshCountdown = document.getElementById("market-refresh-countdown");
    const lastUpdated = document.getElementById("market-last-updated");
    const productCount = document.getElementById("market-product-count");
    const dataState = document.getElementById("market-data-state");
    const searchField = document.getElementById("market-search-field");
    const searchInput = document.getElementById("market-search-input");
    const sortField = document.getElementById("market-sort-field");
    const sortDirection = document.getElementById("market-sort-direction");

    const timestampFormatter = new Intl.DateTimeFormat("en-GB", {
        dateStyle: "medium",
        timeStyle: "medium",
        hour12: false,
        timeZone: "UTC",
    });
    const sortOptions = view === "bazaar"
        ? [
            { value: "title", label: "Alphabetical", type: "text" },
            { value: "spreadPercentage", label: "Spread %", type: "number" },
            { value: "spread", label: "Spread", type: "number" },
            { value: "buy", label: "Buy", type: "number" },
            { value: "sell", label: "Sell", type: "number" },
            { value: "buyVolume", label: "Buy volume", type: "number" },
            { value: "sellVolume", label: "Sell volume", type: "number" },
        ]
        : [
            { value: "title", label: "Alphabetical", type: "text" },
            { value: "price", label: "Price", type: "number" },
            { value: "avg7d", label: "Avg 7d", type: "number" },
            { value: "avg30d", label: "Avg 30d", type: "number" },
        ];
    const defaultSortField = view === "bazaar" ? "spreadPercentage" : "title";
    const defaultSortDirection = view === "bazaar" ? "desc" : "asc";

    let allItems = [];
    let items = [];
    let loadedCount = 0;
    let initialCount = 8;
    let batchSize = 4;
    let isInitialLoad = true;
    let observer = null;
    let refreshTimer = null;
    let countdownTimer = null;
    let fetchPromise = null;
    let nextRefreshAt = Date.now() + refreshIntervalMs;
    const resolvedPlayerNames = new Map();
    const unresolvedPlayerNames = new Set();
    const pendingPlayerNames = new Set();

    function escapeHtml(value) {
        return String(value ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function estimateColumns() {
        const width = window.innerWidth;
        if (width < 768) {
            return 1;
        }
        if (width < 1200) {
            return 2;
        }
        if (width < 1400) {
            return 3;
        }
        return 4;
    }

    function recomputeRenderCounts() {
        const columns = estimateColumns();
        const cardHeight = view === "bazaar" ? 280 : 300;
        const viewportRows = Math.max(1, Math.ceil(window.innerHeight / cardHeight));
        batchSize = Math.max(columns * viewportRows, columns * 2);
        initialCount = Math.max(batchSize * 2, columns * 2);
    }

    function showSkeletons() {
        recomputeRenderCounts();
        const count = initialCount;
        let html = "";
        for (let index = 0; index < count; index += 1) {
            html += `
                <div class="col">
                    <article class="market-placeholder-card" aria-hidden="true">
                        <div class="market-placeholder-pill"></div>
                        <div class="market-placeholder-line is-title"></div>
                        <div class="market-placeholder-line is-subtle"></div>
                        <div class="market-placeholder-line is-price"></div>
                        <div class="market-placeholder-grid">
                            <div class="market-placeholder-block"></div>
                            <div class="market-placeholder-block"></div>
                            <div class="market-placeholder-block"></div>
                            <div class="market-placeholder-block"></div>
                        </div>
                    </article>
                </div>
            `;
        }
        grid.innerHTML = html;
        emptyState.classList.add("d-none");
        sentinel.classList.add("d-none");
    }

    function setAlert(message, level) {
        if (!message) {
            alertSlot.innerHTML = "";
            return;
        }
        alertSlot.innerHTML = `<div class="alert alert-${level}" role="status">${escapeHtml(message)}</div>`;
    }

    function setDataState(state, stale) {
        dataState.className = "badge";
        if (state === "refreshing") {
            dataState.classList.add("market-badge-refreshing");
            dataState.textContent = "Refreshing";
            return;
        }
        if (stale) {
            dataState.classList.add("market-badge-stale");
            dataState.textContent = "Stale data";
            return;
        }
        dataState.classList.add("market-badge-live");
        dataState.textContent = state === "loading" ? "Loading" : "Live";
    }

    function formatCompactNumber(value, digits) {
        if (typeof value !== "number" || !Number.isFinite(value)) {
            return "n/a";
        }
        const absolute = Math.abs(value);
        const precision = typeof digits === "number" ? Math.max(0, digits) : 1;
        if (absolute >= 1000000) {
            return `${trimTrailingZeros((value / 1000000).toFixed(precision))}M`;
        }
        if (absolute >= 1000) {
            return `${trimTrailingZeros((value / 1000).toFixed(precision))}k`;
        }
        if (Number.isInteger(value)) {
            return String(value);
        }
        return trimTrailingZeros(value.toFixed(precision));
    }

    function trimTrailingZeros(value) {
        return value.replace(/\.0+$/, "").replace(/(\.\d*?)0+$/, "$1");
    }

    function formatNumber(value) {
        return formatCompactNumber(value, 1);
    }

    function formatInteger(value) {
        if (typeof value !== "number" || !Number.isFinite(value)) {
            return "n/a";
        }
        return formatCompactNumber(Math.round(value), 0);
    }

    function formatPercent(value) {
        if (typeof value !== "number" || !Number.isFinite(value)) {
            return "n/a";
        }
        return `${formatCompactNumber(value, 1)}%`;
    }

    function formatTimestamp(value) {
        if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
            return "Unknown";
        }
        return `${timestampFormatter.format(new Date(value))} UTC`;
    }

    function renderRefreshCountdown() {
        if (!refreshCountdown) {
            return;
        }
        const remainingMs = Math.max(0, nextRefreshAt - Date.now());
        const remainingSeconds = Math.max(0, Math.ceil(remainingMs / 1000));
        refreshCountdown.textContent = `${remainingSeconds}s`;
    }

    function resetRefreshCountdown() {
        nextRefreshAt = Date.now() + refreshIntervalMs;
        renderRefreshCountdown();
    }

    function setRefreshActive(active) {
        if (!refreshButton) {
            return;
        }
        refreshButton.classList.toggle("is-refreshing", active);
        refreshButton.setAttribute("aria-busy", active ? "true" : "false");
    }

    function updateProductCount() {
        if (!productCount) {
            return;
        }
        const filteredCount = items.length;
        const totalCount = allItems.length;
        productCount.textContent = filteredCount === totalCount
            ? formatInteger(filteredCount)
            : `${formatInteger(filteredCount)}/${formatInteger(totalCount)}`;
    }

    function populateSortOptions() {
        if (!sortField) {
            return;
        }
        sortField.innerHTML = sortOptions
            .map((option) => `<option value="${escapeHtml(option.value)}">${escapeHtml(option.label)}</option>`)
            .join("");
        sortField.value = defaultSortField;
        if (sortDirection) {
            sortDirection.value = defaultSortDirection;
        }
    }

    function buildLowestBinItems(products) {
        return Object.entries(products)
            .map(([key, entry]) => ({
                key,
                title: entry.item_name || key,
                price: entry.price,
                avg7d: entry.avg7d,
                avg30d: entry.avg30d,
                auctioneerUuid: entry.auctioneerUuid || "n/a",
            }))
            .sort((left, right) => {
                const titleCompare = left.title.localeCompare(right.title, undefined, { sensitivity: "base" });
                return titleCompare !== 0 ? titleCompare : left.key.localeCompare(right.key, undefined, { sensitivity: "base" });
            });
    }

    function buildBazaarItems(products) {
        return Object.entries(products)
            .map(([key, entry]) => ({
                key,
                title: entry.item_name || key,
                buy: entry.buy,
                sell: entry.sell,
                spread: entry.spread,
                spreadPercentage: entry.spreadPercentage,
                buyVolume: entry.buyVolume,
                sellVolume: entry.sellVolume,
            }))
            .sort((left, right) => {
                const spreadDiff = Number(right.spreadPercentage || 0) - Number(left.spreadPercentage || 0);
                if (spreadDiff !== 0) {
                    return spreadDiff;
                }
                return left.key.localeCompare(right.key, undefined, { sensitivity: "base" });
            });
    }

    function normalizePayload(payload) {
        const safePayload = payload && typeof payload === "object" ? payload : {};
        const products = safePayload.products && typeof safePayload.products === "object" ? safePayload.products : {};
        return {
            lastUpdated: Number(safePayload.lastUpdated || 0),
            items: view === "bazaar" ? buildBazaarItems(products) : buildLowestBinItems(products),
        };
    }

    function compareItems(left, right, option, direction) {
        const multiplier = direction === "desc" ? -1 : 1;
        if (!option || option.type === "text") {
            const leftValue = String(left?.[option?.value || "title"] || "");
            const rightValue = String(right?.[option?.value || "title"] || "");
            const primary = leftValue.localeCompare(rightValue, undefined, { sensitivity: "base" });
            if (primary !== 0) {
                return primary * multiplier;
            }
            return left.key.localeCompare(right.key, undefined, { sensitivity: "base" }) * multiplier;
        }

        const leftValue = Number(left?.[option.value]);
        const rightValue = Number(right?.[option.value]);
        const leftMissing = !Number.isFinite(leftValue);
        const rightMissing = !Number.isFinite(rightValue);
        if (leftMissing && rightMissing) {
            return left.key.localeCompare(right.key, undefined, { sensitivity: "base" }) * multiplier;
        }
        if (leftMissing) {
            return 1;
        }
        if (rightMissing) {
            return -1;
        }
        const numericDiff = leftValue - rightValue;
        if (numericDiff !== 0) {
            return numericDiff * multiplier;
        }
        return left.key.localeCompare(right.key, undefined, { sensitivity: "base" }) * multiplier;
    }

    function applyFiltersAndSort(options) {
        const resetLoadedCount = Boolean(options && options.resetLoadedCount);
        const selectedSearchField = searchField?.value || "title";
        const query = String(searchInput?.value || "").trim().toLowerCase();
        const selectedSortField = sortField?.value || defaultSortField;
        const selectedSortDirection = sortDirection?.value || defaultSortDirection;
        const selectedOption = sortOptions.find((option) => option.value === selectedSortField) || sortOptions[0];

        items = allItems
            .filter((item) => {
                if (!query) {
                    return true;
                }
                const candidate = String(item?.[selectedSearchField] || "").toLowerCase();
                return candidate.includes(query);
            })
            .sort((left, right) => compareItems(left, right, selectedOption, selectedSortDirection));

        updateProductCount();
        recomputeRenderCounts();
        if (resetLoadedCount || loadedCount === 0) {
            loadedCount = Math.min(initialCount, items.length);
        } else {
            loadedCount = Math.min(items.length, Math.max(loadedCount, initialCount));
        }
        render();
    }

    function lowestBinCard(item) {
        const auctioneerName = resolvedPlayerNames.get(String(item.auctioneerUuid || "").toLowerCase()) || "";
        return `
            <div class="col">
                <article class="market-card h-100">
                    <div class="market-card-header">
                        <div>
                            <h3 class="market-card-title">${escapeHtml(item.title)}</h3>
                            <div class="market-card-key">${escapeHtml(item.key)}</div>
                        </div>
                    </div>
                    <div class="market-price">${escapeHtml(formatNumber(item.price))}</div>
                    <div class="market-card-grid">
                        <div class="market-stat">
                            <span class="market-stat-label">Current price</span>
                            <span class="market-stat-value">${escapeHtml(formatNumber(item.price))}</span>
                        </div>
                        <div class="market-stat">
                            <span class="market-stat-label">Avg 7d</span>
                            <span class="market-stat-value">${escapeHtml(formatInteger(item.avg7d))}</span>
                        </div>
                        <div class="market-stat">
                            <span class="market-stat-label">Avg 30d</span>
                            <span class="market-stat-value">${escapeHtml(formatInteger(item.avg30d))}</span>
                        </div>
                        <div class="market-stat">
                            <span class="market-stat-label">Market view</span>
                            <span class="market-stat-value">Auction BIN</span>
                        </div>
                    </div>
                    <div class="market-uuid">
                        <strong>Auctioneer</strong><br>
                        <span class="market-auctioneer-name" data-auctioneer-uuid="${escapeHtml(item.auctioneerUuid)}">
                            ${escapeHtml(auctioneerName || item.auctioneerUuid)}
                        </span>
                    </div>
                </article>
            </div>
        `;
    }

    function bazaarCard(item) {
        return `
            <div class="col">
                <article class="market-card h-100">
                    <div class="market-card-header">
                        <div>
                            <h3 class="market-card-title">${escapeHtml(item.title)}</h3>
                            <div class="market-card-key">${escapeHtml(item.key)}</div>
                        </div>
                    </div>
                    <div class="market-price">${escapeHtml(formatPercent(item.spreadPercentage))}</div>
                    <div class="market-card-grid">
                        <div class="market-stat">
                            <span class="market-stat-label">Buy</span>
                            <span class="market-stat-value">${escapeHtml(formatNumber(item.buy))}</span>
                        </div>
                        <div class="market-stat">
                            <span class="market-stat-label">Sell</span>
                            <span class="market-stat-value">${escapeHtml(formatNumber(item.sell))}</span>
                        </div>
                        <div class="market-stat">
                            <span class="market-stat-label">Spread</span>
                            <span class="market-stat-value">${escapeHtml(formatNumber(item.spread))}</span>
                        </div>
                        <div class="market-stat">
                            <span class="market-stat-label">Spread %</span>
                            <span class="market-stat-value">${escapeHtml(formatPercent(item.spreadPercentage))}</span>
                        </div>
                        <div class="market-stat">
                            <span class="market-stat-label">Buy volume</span>
                            <span class="market-stat-value">${escapeHtml(formatInteger(item.buyVolume))}</span>
                        </div>
                        <div class="market-stat">
                            <span class="market-stat-label">Sell volume</span>
                            <span class="market-stat-value">${escapeHtml(formatInteger(item.sellVolume))}</span>
                        </div>
                    </div>
                </article>
            </div>
        `;
    }

    function render() {
        if (items.length === 0) {
            grid.innerHTML = "";
            emptyState.classList.remove("d-none");
            if (emptyMessage) {
                emptyMessage.textContent = String(searchInput?.value || "").trim()
                    ? "No matching market entries were found."
                    : "No market entries are available right now.";
            }
            sentinel.classList.add("d-none");
            return;
        }

        emptyState.classList.add("d-none");
        const visibleItems = items.slice(0, loadedCount);
        const renderer = view === "bazaar" ? bazaarCard : lowestBinCard;
        grid.innerHTML = visibleItems.map(renderer).join("");
        sentinel.classList.toggle("d-none", loadedCount >= items.length);
        if (view === "lowestbin") {
            void resolveVisiblePlayerNames();
        }
    }

    function loadMore() {
        if (loadedCount >= items.length) {
            sentinel.classList.add("d-none");
            return;
        }
        loadedCount = Math.min(items.length, loadedCount + batchSize);
        render();
    }

    function installObserver() {
        if (observer) {
            observer.disconnect();
        }
        observer = new IntersectionObserver(
            (entries) => {
                for (const entry of entries) {
                    if (entry.isIntersecting) {
                        loadMore();
                    }
                }
            },
            {
                rootMargin: "640px 0px 640px 0px",
            }
        );
        observer.observe(sentinel);
    }

    async function resolveVisiblePlayerNames() {
        const targets = Array.from(grid.querySelectorAll("[data-auctioneer-uuid]"));
        const uuids = [];
        for (const target of targets) {
            const rawUuid = String(target.getAttribute("data-auctioneer-uuid") || "").trim().toLowerCase();
            if (!rawUuid || resolvedPlayerNames.has(rawUuid) || unresolvedPlayerNames.has(rawUuid) || pendingPlayerNames.has(rawUuid)) {
                continue;
            }
            pendingPlayerNames.add(rawUuid);
            uuids.push(rawUuid);
            if (uuids.length >= 24) {
                break;
            }
        }

        if (uuids.length === 0) {
            syncResolvedPlayerNames();
            return;
        }

        try {
            const response = await fetch(`${basePath}/api/player-names`, {
                method: "POST",
                headers: {
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                cache: "no-store",
                body: JSON.stringify({ uuids }),
            });
            if (!response.ok) {
                return;
            }
            const payload = await response.json();
            const names = payload && typeof payload === "object" ? payload.playerNames : null;
            if (!names || typeof names !== "object") {
                return;
            }
            const resolvedThisRound = new Set();
            for (const [rawUuid, rawName] of Object.entries(names)) {
                const normalizedUuid = String(rawUuid || "").trim().toLowerCase();
                const normalizedName = String(rawName || "").trim();
                if (normalizedUuid && normalizedName) {
                    resolvedPlayerNames.set(normalizedUuid, normalizedName);
                    unresolvedPlayerNames.delete(normalizedUuid);
                    resolvedThisRound.add(normalizedUuid);
                }
            }
            for (const playerUuid of uuids) {
                if (!resolvedThisRound.has(playerUuid)) {
                    unresolvedPlayerNames.add(playerUuid);
                }
            }
        } catch (_error) {
            // Keep UUID fallback rendering if the name lookup is unavailable.
        } finally {
            for (const playerUuid of uuids) {
                pendingPlayerNames.delete(playerUuid);
            }
            syncResolvedPlayerNames();
        }
    }

    function syncResolvedPlayerNames() {
        const targets = Array.from(grid.querySelectorAll("[data-auctioneer-uuid]"));
        for (const target of targets) {
            const rawUuid = String(target.getAttribute("data-auctioneer-uuid") || "").trim().toLowerCase();
            if (!rawUuid) {
                continue;
            }
            const playerName = resolvedPlayerNames.get(rawUuid);
            target.textContent = playerName || rawUuid;
        }
    }

    async function fetchMarketData(options) {
        const background = Boolean(options && options.background);
        if (fetchPromise) {
            return fetchPromise;
        }

        if (background) {
            setDataState("refreshing", false);
        } else {
            setDataState("loading", false);
            if (isInitialLoad) {
                showSkeletons();
            }
        }
        setRefreshActive(true);

        fetchPromise = fetch(endpoint, {
            headers: {
                Accept: "application/json",
            },
            cache: "no-store",
        })
            .then(async (response) => {
                const stale = String(response.headers.get("x-data-stale") || "").toLowerCase() === "true";
                const retryAfter = response.headers.get("retry-after");

                if (!response.ok) {
                    let message = "Market data is temporarily unavailable.";
                    try {
                        const payload = await response.json();
                        if (payload && typeof payload.detail === "string" && payload.detail.trim()) {
                            message = payload.detail.trim();
                        }
                    } catch (_error) {
                        // Ignore malformed error payloads and keep the user-safe message.
                    }
                    if (response.status === 429 && retryAfter) {
                        message = `Please wait about ${retryAfter} seconds before trying again.`;
                    }
                    throw new Error(message);
                }

                const payload = await response.json();
                const normalized = normalizePayload(payload);
                allItems = normalized.items;
                applyFiltersAndSort({ resetLoadedCount: isInitialLoad || loadedCount === 0 });
                lastUpdated.textContent = formatTimestamp(normalized.lastUpdated);
                setAlert("", "secondary");
                setDataState("live", stale);
                isInitialLoad = false;
                resetRefreshCountdown();
                return true;
            })
            .catch((error) => {
                if (items.length > 0) {
                    setAlert(error instanceof Error ? error.message : "Market data could not be refreshed.", "warning");
                    setDataState("live", true);
                } else {
                    grid.innerHTML = "";
                    emptyState.classList.add("d-none");
                    setAlert(error instanceof Error ? error.message : "Market data could not be loaded.", "secondary");
                    lastUpdated.textContent = "Unavailable";
                    allItems = [];
                    items = [];
                    updateProductCount();
                    setDataState("live", true);
                }
                return false;
            })
            .finally(() => {
                setRefreshActive(false);
                fetchPromise = null;
            });

        return fetchPromise;
    }

    function handleResize() {
        const previousInitial = initialCount;
        recomputeRenderCounts();
        if (items.length > 0 && loadedCount < initialCount && initialCount > previousInitial) {
            loadedCount = Math.min(items.length, initialCount);
            render();
        }
    }

    refreshButton.addEventListener("click", function () {
        void fetchMarketData({ background: items.length > 0 });
    });
    searchField?.addEventListener("change", function () {
        applyFiltersAndSort({ resetLoadedCount: true });
    });
    searchInput?.addEventListener("input", function () {
        applyFiltersAndSort({ resetLoadedCount: true });
    });
    sortField?.addEventListener("change", function () {
        applyFiltersAndSort({ resetLoadedCount: true });
    });
    sortDirection?.addEventListener("change", function () {
        applyFiltersAndSort({ resetLoadedCount: true });
    });
    window.addEventListener("resize", handleResize, { passive: true });

    recomputeRenderCounts();
    populateSortOptions();
    installObserver();
    showSkeletons();
    renderRefreshCountdown();
    void fetchMarketData({ background: false });
    refreshTimer = window.setInterval(function () {
        void fetchMarketData({ background: true });
    }, refreshIntervalMs);
    countdownTimer = window.setInterval(renderRefreshCountdown, 1000);

    window.addEventListener("beforeunload", function () {
        if (refreshTimer) {
            window.clearInterval(refreshTimer);
        }
        if (countdownTimer) {
            window.clearInterval(countdownTimer);
        }
        if (observer) {
            observer.disconnect();
        }
    });
}());
