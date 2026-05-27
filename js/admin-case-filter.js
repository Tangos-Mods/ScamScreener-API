(function () {
    "use strict";

    function normalizedValues(attributeValue) {
        return Array.from(
            new Set(
            String(attributeValue || "")
                .split(/\s+/)
                .map(function (value) {
                    return String(value || "").trim().toLowerCase();
                })
                .filter(Boolean),
            ),
        );
    }

    function parseStructuredToken(segment, validStatuses, validLabels) {
        const normalized = String(segment || "").trim().toLowerCase();
        if (!normalized) {
            return null;
        }
        const parts = normalized.split(":", 2);
        if (parts.length !== 2) {
            return null;
        }
        const key = parts[0];
        const value = parts[1];
        if (key === "status" && validStatuses.includes(value)) {
            return key + ":" + value;
        }
        if (key === "label" && validLabels.includes(value)) {
            return key + ":" + value;
        }
        return null;
    }

    function renderBadges(container, tokens) {
        container.replaceChildren();
        tokens.forEach(function (token) {
            const badge = document.createElement("span");
            badge.className = "admin-case-filter-badge";
            badge.textContent = token;
            container.appendChild(badge);
        });
    }

    function composeFilterValue(tokens, freeText) {
        const parts = tokens.slice();
        const normalizedText = String(freeText || "").trim();
        if (normalizedText) {
            parts.push(normalizedText);
        }
        return parts.join(", ");
    }

    function splitInputSegments(rawValue) {
        const inputValue = String(rawValue || "");
        const lastCommaIndex = inputValue.lastIndexOf(",");
        if (lastCommaIndex === -1) {
            return { prefix: "", activeSegment: inputValue };
        }
        return {
            prefix: inputValue.slice(0, lastCommaIndex + 1),
            activeSegment: inputValue.slice(lastCommaIndex + 1),
        };
    }

    function createSuggestion(value, meta) {
        return { value: value, meta: meta };
    }

    function getSuggestions(activeSegment, validStatuses, validLabels) {
        const normalizedSegment = String(activeSegment || "").trim().toLowerCase();
        if (!normalizedSegment) {
            return [
                createSuggestion("status:", "Filter by workflow status"),
                createSuggestion("label:", "Filter by moderation label"),
            ];
        }

        if ("status:".startsWith(normalizedSegment)) {
            return [createSuggestion("status:", "Filter by workflow status")];
        }
        if ("label:".startsWith(normalizedSegment)) {
            return [createSuggestion("label:", "Filter by moderation label")];
        }
        if (normalizedSegment.startsWith("status:")) {
            const valuePrefix = normalizedSegment.slice("status:".length);
            return validStatuses
                .filter(function (status) {
                    return status.startsWith(valuePrefix);
                })
                .map(function (status) {
                    return createSuggestion("status:" + status, "Status");
                });
        }
        if (normalizedSegment.startsWith("label:")) {
            const valuePrefix = normalizedSegment.slice("label:".length);
            return validLabels
                .filter(function (label) {
                    return label.startsWith(valuePrefix);
                })
                .map(function (label) {
                    return createSuggestion("label:" + label, "Label");
                });
        }
        return [];
    }

    function setExpandedState(textInput, isExpanded) {
        textInput.setAttribute("aria-expanded", isExpanded ? "true" : "false");
        if (!isExpanded) {
            textInput.removeAttribute("aria-activedescendant");
        }
    }

    function tokenizeCompletedSegments(rawValue, tokens, validStatuses, validLabels) {
        const segments = String(rawValue || "").split(",");
        if (segments.length <= 1) {
            return String(rawValue || "");
        }

        const remainderSegments = [];
        segments.forEach(function (segment, index) {
            const isLast = index === segments.length - 1;
            const trimmed = String(segment || "").trim();
            if (isLast) {
                if (trimmed) {
                    remainderSegments.push(trimmed);
                }
                return;
            }
            if (!trimmed) {
                return;
            }
            const structuredToken = parseStructuredToken(trimmed, validStatuses, validLabels);
            if (structuredToken) {
                tokens.push(structuredToken);
            } else {
                remainderSegments.push(trimmed);
            }
        });
        return remainderSegments.join(", ");
    }

    function initCaseFilter(form) {
        const badgeContainer = form.querySelector("[data-case-filter-badges]");
        const textInput = form.querySelector("[data-case-filter-input]");
        const hiddenInput = form.querySelector("[data-case-filter-hidden]");
        const suggestionContainer = form.querySelector("[data-case-filter-suggestions]");
        if (!badgeContainer || !textInput || !hiddenInput || !suggestionContainer) {
            return;
        }

        const validStatuses = normalizedValues(form.getAttribute("data-valid-statuses"));
        const validLabels = normalizedValues(form.getAttribute("data-valid-labels"));
        const tokens = [];
        let activeSuggestions = [];
        let activeSuggestionIndex = -1;
        let autoSubmitTimerId = 0;
        let lastSubmittedValue = String(hiddenInput.value || "").trim();

        function syncHiddenValue() {
            hiddenInput.value = composeFilterValue(tokens, textInput.value);
        }

        function syncBadges() {
            renderBadges(badgeContainer, tokens);
            syncHiddenValue();
        }

        function processInputValue() {
            textInput.value = tokenizeCompletedSegments(textInput.value, tokens, validStatuses, validLabels);
            syncBadges();
        }

        function clearAutoSubmitTimer() {
            if (!autoSubmitTimerId) {
                return;
            }
            window.clearTimeout(autoSubmitTimerId);
            autoSubmitTimerId = 0;
        }

        function submitIfChanged() {
            const nextValue = String(hiddenInput.value || "").trim();
            if (nextValue === lastSubmittedValue) {
                return;
            }
            clearAutoSubmitTimer();
            lastSubmittedValue = nextValue;
            form.requestSubmit();
        }

        function scheduleAutoSubmit(delayMs) {
            clearAutoSubmitTimer();
            autoSubmitTimerId = window.setTimeout(function () {
                autoSubmitTimerId = 0;
                submitIfChanged();
            }, delayMs);
        }

        function closeSuggestions() {
            activeSuggestions = [];
            activeSuggestionIndex = -1;
            suggestionContainer.replaceChildren();
            suggestionContainer.hidden = true;
            setExpandedState(textInput, false);
        }

        function updateActiveSuggestion() {
            const buttons = suggestionContainer.querySelectorAll("[data-suggestion-index]");
            buttons.forEach(function (button, index) {
                const isActive = index === activeSuggestionIndex;
                button.classList.toggle("is-active", isActive);
                button.setAttribute("aria-selected", isActive ? "true" : "false");
                if (isActive) {
                    textInput.setAttribute("aria-activedescendant", button.id);
                }
            });
            if (activeSuggestionIndex < 0) {
                textInput.removeAttribute("aria-activedescendant");
            }
        }

        function applySuggestion(suggestionValue) {
            const segments = splitInputSegments(textInput.value);
            const preservedPrefix = segments.prefix;
            const prefixWithSpacer = preservedPrefix && !/\s$/.test(preservedPrefix) ? preservedPrefix + " " : preservedPrefix;
            textInput.value = prefixWithSpacer + suggestionValue + ", ";
            processInputValue();
            closeSuggestions();
            textInput.focus();
            submitIfChanged();
        }

        function renderSuggestions() {
            const segments = splitInputSegments(textInput.value);
            activeSuggestions = getSuggestions(segments.activeSegment, validStatuses, validLabels);
            suggestionContainer.replaceChildren();

            if (activeSuggestions.length === 0) {
                closeSuggestions();
                return;
            }

            activeSuggestionIndex = 0;
            activeSuggestions.forEach(function (suggestion, index) {
                const option = document.createElement("button");
                option.type = "button";
                option.className = "admin-case-filter-suggestion";
                option.id = "admin-case-filter-suggestion-" + index;
                option.setAttribute("role", "option");
                option.setAttribute("data-suggestion-index", String(index));
                option.setAttribute("aria-selected", index === activeSuggestionIndex ? "true" : "false");
                option.innerHTML =
                    '<span class="admin-case-filter-suggestion-value"></span>' +
                    '<span class="admin-case-filter-suggestion-meta"></span>';
                option.querySelector(".admin-case-filter-suggestion-value").textContent = suggestion.value;
                option.querySelector(".admin-case-filter-suggestion-meta").textContent = suggestion.meta;
                option.addEventListener("mousedown", function (event) {
                    event.preventDefault();
                });
                option.addEventListener("click", function () {
                    applySuggestion(suggestion.value);
                });
                suggestionContainer.appendChild(option);
            });

            suggestionContainer.hidden = false;
            setExpandedState(textInput, true);
            updateActiveSuggestion();
        }

        const initialValue = String(hiddenInput.value || "").trim();
        if (initialValue) {
            const segments = initialValue.split(",");
            const remainingSegments = [];
            segments.forEach(function (segment) {
                const trimmed = String(segment || "").trim();
                if (!trimmed) {
                    return;
                }
                const structuredToken = parseStructuredToken(trimmed, validStatuses, validLabels);
                if (structuredToken) {
                    tokens.push(structuredToken);
                } else {
                    remainingSegments.push(trimmed);
                }
            });
            textInput.value = remainingSegments.join(", ");
        }
        syncBadges();
        closeSuggestions();

        textInput.addEventListener("input", function () {
            const previousTokenCount = tokens.length;
            processInputValue();
            renderSuggestions();
            if (tokens.length !== previousTokenCount) {
                submitIfChanged();
                return;
            }
            scheduleAutoSubmit(350);
        });
        textInput.addEventListener("keydown", function (event) {
            if (event.key === "ArrowDown" && activeSuggestions.length > 0) {
                event.preventDefault();
                activeSuggestionIndex = (activeSuggestionIndex + 1) % activeSuggestions.length;
                updateActiveSuggestion();
                return;
            }
            if (event.key === "ArrowUp" && activeSuggestions.length > 0) {
                event.preventDefault();
                activeSuggestionIndex = (activeSuggestionIndex - 1 + activeSuggestions.length) % activeSuggestions.length;
                updateActiveSuggestion();
                return;
            }
            if ((event.key === "Enter" || event.key === "Tab") && activeSuggestions.length > 0 && activeSuggestionIndex >= 0) {
                event.preventDefault();
                applySuggestion(activeSuggestions[activeSuggestionIndex].value);
                return;
            }
            if (event.key === "Escape") {
                closeSuggestions();
                return;
            }
            if (event.key !== "Backspace") {
                return;
            }
            if (textInput.value) {
                return;
            }
            if (tokens.length === 0) {
                return;
            }
            event.preventDefault();
            tokens.pop();
            syncBadges();
            submitIfChanged();
        });
        textInput.addEventListener("focus", renderSuggestions);
        textInput.addEventListener("blur", function () {
            syncHiddenValue();
            submitIfChanged();
            window.setTimeout(closeSuggestions, 100);
        });
        form.addEventListener("submit", function () {
            clearAutoSubmitTimer();
            processInputValue();
            syncHiddenValue();
            lastSubmittedValue = String(hiddenInput.value || "").trim();
        });
        document.addEventListener("click", function (event) {
            if (!form.contains(event.target)) {
                closeSuggestions();
            }
        });
    }

    document.querySelectorAll("[data-case-filter]").forEach(initCaseFilter);
}());
