(function () {
    const root = document.getElementById("live-api-metrics");
    if (!root) {
        return;
    }

    const streamUrl = root.dataset.streamUrl || "";
    const initialSnapshotRaw = root.dataset.initialSnapshot || "{}";
    const endpointRows = document.getElementById("live-endpoint-rows");
    const animationHandles = new WeakMap();
    const rowCache = new Map();
    const fields = {
        totalToday: document.getElementById("live-total-today"),
        total10s: document.getElementById("live-total-10s"),
        total60s: document.getElementById("live-total-60s"),
        totalAllTime: document.getElementById("live-total-all-time"),
        publicToday: document.getElementById("live-public-today"),
        public10s: document.getElementById("live-public-10s"),
        publicRps: document.getElementById("live-public-rps"),
        public60s: document.getElementById("live-public-60s"),
        publicTotal: document.getElementById("live-public-total"),
        clientToday: document.getElementById("live-client-today"),
        client10s: document.getElementById("live-client-10s"),
        clientRps: document.getElementById("live-client-rps"),
        client60s: document.getElementById("live-client-60s"),
        clientTotal: document.getElementById("live-client-total"),
        internalToday: document.getElementById("live-internal-today"),
        internal10s: document.getElementById("live-internal-10s"),
        internalRps: document.getElementById("live-internal-rps"),
        internal60s: document.getElementById("live-internal-60s"),
        internalTotal: document.getElementById("live-internal-total"),
    };

    function groupThousands(value) {
        return String(value).replace(/\B(?=(\d{3})+(?!\d))/g, ".");
    }

    function formatInteger(value) {
        const numeric = Number(value);
        if (!Number.isFinite(numeric)) {
            return "0";
        }
        return groupThousands(String(Math.round(numeric)));
    }

    function formatRate(value) {
        const numeric = Number(value);
        return Number.isFinite(numeric) ? numeric.toFixed(2) : "0.00";
    }

    function clearAnimationHandle(element) {
        const handle = animationHandles.get(element);
        if (handle) {
            if (Array.isArray(handle.frames)) {
                handle.frames.forEach(function (frameHandle) {
                    window.cancelAnimationFrame(frameHandle);
                });
            }
            if (handle.timeout) {
                window.clearTimeout(handle.timeout);
            }
            animationHandles.delete(element);
        }
    }

    function isDigitCharacter(value) {
        return value >= "0" && value <= "9";
    }

    function buildDigitOdometer(previousFormatted, nextFormatted, animateIncrease) {
        const wrapper = document.createElement("span");
        wrapper.className = "metric-odometer";
        wrapper.setAttribute("aria-hidden", "true");

        const previousPadded = String(previousFormatted || "").padStart(nextFormatted.length, " ");
        const nextPadded = String(nextFormatted || "").padStart(nextFormatted.length, " ");
        const animations = [];

        for (let index = 0; index < nextPadded.length; index += 1) {
            const previousCharacter = previousPadded[index];
            const nextCharacter = nextPadded[index];
            if (nextCharacter === " ") {
                continue;
            }

            if (!isDigitCharacter(previousCharacter) || !isDigitCharacter(nextCharacter) || previousCharacter === nextCharacter) {
                const staticNode = document.createElement("span");
                staticNode.className = "metric-odometer-static";
                staticNode.textContent = nextCharacter;
                wrapper.appendChild(staticNode);
                continue;
            }

            const slotNode = document.createElement("span");
            slotNode.className = "metric-odometer-slot";
            const trackNode = document.createElement("span");
            trackNode.className = "metric-odometer-track";

            const firstNode = document.createElement("span");
            firstNode.className = "metric-odometer-value";
            const secondNode = document.createElement("span");
            secondNode.className = "metric-odometer-value";

            if (animateIncrease) {
                firstNode.textContent = nextCharacter;
                secondNode.textContent = previousCharacter;
                trackNode.style.transform = "translateY(-50%)";
            } else {
                firstNode.textContent = previousCharacter;
                secondNode.textContent = nextCharacter;
                trackNode.style.transform = "translateY(0)";
            }

            trackNode.appendChild(firstNode);
            trackNode.appendChild(secondNode);
            slotNode.appendChild(trackNode);
            wrapper.appendChild(slotNode);
            animations.push({ slotNode: slotNode, trackNode: trackNode });
        }

        return { wrapper: wrapper, animations: animations };
    }

    function setAnimatedNumber(element, value, animate, decimals, groupThousandsEnabled) {
        if (!element) {
            return;
        }
        const target = Number(value);
        if (!Number.isFinite(target)) {
            element.textContent = decimals > 0 ? `0.${"0".repeat(decimals)}` : "0";
            element.dataset.rawValue = "0";
            return;
        }
        const precision = Math.max(0, Number(decimals) || 0);
        const scaledFactor = Math.pow(10, precision);
        const nextValue = Math.round(target * scaledFactor);
        const previousValue = Number(element.dataset.rawValue || nextValue);
        element.dataset.rawValue = String(nextValue);
        const formatted = precision > 0 ? formatRate(target) : formatInteger(target);
        if (!animate || previousValue === nextValue) {
            clearAnimationHandle(element);
            element.textContent = formatted;
            element.setAttribute("aria-label", formatted);
            return;
        }

        clearAnimationHandle(element);

        const animateIncrease = nextValue > previousValue;
        const previousFormatted = precision > 0
            ? formatRate(previousValue / scaledFactor)
            : formatInteger(previousValue / scaledFactor);
        const odometer = buildDigitOdometer(previousFormatted, formatted, animateIncrease);
        element.replaceChildren(odometer.wrapper);
        element.setAttribute("aria-label", formatted);
        if (!odometer.animations.length) {
            return;
        }

        odometer.wrapper.getBoundingClientRect();
        const handle = { frames: [], timeout: 0 };
        const firstFrame = window.requestAnimationFrame(function () {
            const secondFrame = window.requestAnimationFrame(function () {
                odometer.animations.forEach(function (animation, index) {
                    animation.slotNode.classList.add("is-animating");
                    animation.slotNode.classList.toggle("is-increase", animateIncrease);
                    animation.slotNode.classList.toggle("is-decrease", !animateIncrease);
                    const trackNode = animation.trackNode;
                    trackNode.style.transitionDelay = `${Math.min(index * 28, 140)}ms`;
                    trackNode.style.transform = animateIncrease ? "translateY(0)" : "translateY(-50%)";
                });
            });
            handle.frames.push(secondFrame);
        });
        handle.frames.push(firstFrame);
        handle.timeout = window.setTimeout(function () {
            element.textContent = formatted;
            element.setAttribute("aria-label", formatted);
            animationHandles.delete(element);
        }, 620);
        animationHandles.set(element, handle);
    }

    function setAnimatedInteger(element, value, animate) {
        setAnimatedNumber(element, value, animate, 0, true);
    }

    function setRateField(element, value, animate) {
        setAnimatedNumber(element, value, animate, 2, false);
    }

    function ensureEndpointRow(entry) {
        const key = `${entry.endpoint}\u0000${entry.agent}`;
        let row = rowCache.get(key);
        if (row) {
            return row;
        }
        row = document.createElement("tr");
        row.dataset.key = key;

        const endpointCell = document.createElement("td");
        endpointCell.className = "table-wrap";
        endpointCell.textContent = String(entry.endpoint || "");
        row.appendChild(endpointCell);

        const last10sCell = document.createElement("td");
        last10sCell.dataset.role = "requestsLast10s";
        row.appendChild(last10sCell);

        const last60sCell = document.createElement("td");
        last60sCell.dataset.role = "requestsLast60s";
        row.appendChild(last60sCell);

        const todayCell = document.createElement("td");
        todayCell.dataset.role = "requestsToday";
        row.appendChild(todayCell);

        const totalCell = document.createElement("td");
        totalCell.dataset.role = "totalSinceStart";
        row.appendChild(totalCell);

        const agentCell = document.createElement("td");
        agentCell.className = "table-wrap";
        agentCell.textContent = String(entry.agent || "");
        row.appendChild(agentCell);

        rowCache.set(key, row);
        return row;
    }

    function applyEndpointRows(entries, animate) {
        if (!endpointRows) {
            return;
        }
        if (entries.length === 0) {
            rowCache.clear();
            endpointRows.innerHTML = '<tr><td colspan="6" class="table-empty">No API traffic recorded yet.</td></tr>';
            return;
        }

        endpointRows.innerHTML = "";
        const activeKeys = new Set();
        entries.forEach(function (entry) {
            const row = ensureEndpointRow(entry);
            const key = row.dataset.key || "";
            activeKeys.add(key);
            row.children[0].textContent = String(entry.endpoint || "");
            row.children[5].textContent = String(entry.agent || "");
            setAnimatedInteger(row.querySelector('[data-role="requestsLast10s"]'), entry.requestsLast10s, animate);
            setAnimatedInteger(row.querySelector('[data-role="requestsLast60s"]'), entry.requestsLast60s, animate);
            setAnimatedInteger(row.querySelector('[data-role="requestsToday"]'), entry.requestsToday, animate);
            setAnimatedInteger(row.querySelector('[data-role="totalSinceStart"]'), entry.totalSinceStart, animate);
            endpointRows.appendChild(row);
        });

        Array.from(rowCache.keys()).forEach(function (key) {
            if (!activeKeys.has(key)) {
                rowCache.delete(key);
            }
        });
    }

    function applySnapshot(snapshot, animate) {
        const publicApi = snapshot.publicApi || {};
        const clientApi = snapshot.clientApi || {};
        const internalApi = snapshot.internalApi || {};
        const entries = Array.isArray(snapshot.entries) ? snapshot.entries : [];

        setAnimatedInteger(fields.totalToday, snapshot.totalToday, animate);
        setAnimatedInteger(fields.total10s, snapshot.totalLast10s, animate);
        setAnimatedInteger(fields.total60s, snapshot.totalLast60s, animate);
        setAnimatedInteger(fields.totalAllTime, snapshot.totalSinceStart, animate);

        setAnimatedInteger(fields.publicToday, publicApi.requestsToday, animate);
        setAnimatedInteger(fields.public10s, publicApi.requestsLast10s, animate);
        setRateField(fields.publicRps, publicApi.requestsPerSecond10s, animate);
        setAnimatedInteger(fields.public60s, publicApi.requestsLast60s, animate);
        setAnimatedInteger(fields.publicTotal, publicApi.totalSinceStart, animate);

        setAnimatedInteger(fields.clientToday, clientApi.requestsToday, animate);
        setAnimatedInteger(fields.client10s, clientApi.requestsLast10s, animate);
        setRateField(fields.clientRps, clientApi.requestsPerSecond10s, animate);
        setAnimatedInteger(fields.client60s, clientApi.requestsLast60s, animate);
        setAnimatedInteger(fields.clientTotal, clientApi.totalSinceStart, animate);

        setAnimatedInteger(fields.internalToday, internalApi.requestsToday, animate);
        setAnimatedInteger(fields.internal10s, internalApi.requestsLast10s, animate);
        setRateField(fields.internalRps, internalApi.requestsPerSecond10s, animate);
        setAnimatedInteger(fields.internal60s, internalApi.requestsLast60s, animate);
        setAnimatedInteger(fields.internalTotal, internalApi.totalSinceStart, animate);

        applyEndpointRows(entries, animate);
    }

    try {
        applySnapshot(JSON.parse(initialSnapshotRaw), false);
    } catch (error) {}

    if (!streamUrl || typeof window.EventSource !== "function") {
        return;
    }

    let eventSource = null;
    let reconnectTimer = 0;

    function clearReconnectTimer() {
        if (reconnectTimer) {
            window.clearTimeout(reconnectTimer);
            reconnectTimer = 0;
        }
    }

    function scheduleReconnect() {
        if (reconnectTimer) {
            return;
        }
        reconnectTimer = window.setTimeout(function () {
            reconnectTimer = 0;
            connectStream();
        }, 1500);
    }

    function connectStream() {
        clearReconnectTimer();
        if (eventSource) {
            eventSource.close();
        }
        eventSource = new EventSource(streamUrl, { withCredentials: true });

        eventSource.addEventListener("snapshot", function (event) {
            try {
                applySnapshot(JSON.parse(event.data || "{}"), true);
            } catch (error) {}
        });

        eventSource.addEventListener("error", function () {
            if (!eventSource || eventSource.readyState === window.EventSource.OPEN) {
                return;
            }
            eventSource.close();
            scheduleReconnect();
        });
    }

    connectStream();
})();
