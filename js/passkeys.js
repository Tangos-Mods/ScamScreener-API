(function () {
    "use strict";

    function base64urlToBytes(value) {
        const normalized = String(value || "");
        const padded = normalized + "=".repeat((4 - (normalized.length % 4 || 4)) % 4);
        const base64 = padded.replace(/-/g, "+").replace(/_/g, "/");
        const raw = atob(base64);
        return Uint8Array.from(raw, function (char) {
            return char.charCodeAt(0);
        });
    }

    function bytesToBase64url(buffer) {
        const bytes = new Uint8Array(buffer);
        let raw = "";
        for (const byte of bytes) {
            raw += String.fromCharCode(byte);
        }
        return btoa(raw).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
    }

    function clonePublicKeyOptions(publicKey, contextLabel) {
        if (!publicKey || typeof publicKey !== "object") {
            throw new Error(contextLabel + " did not return valid credential options.");
        }
        if (typeof window.structuredClone === "function") {
            return window.structuredClone(publicKey);
        }
        return JSON.parse(JSON.stringify(publicKey));
    }

    function requireFlowPayload(optionsPayload, contextLabel) {
        if (optionsPayload && typeof optionsPayload.redirectUrl === "string" && optionsPayload.redirectUrl) {
            window.location.href = optionsPayload.redirectUrl;
            return null;
        }
        if (!optionsPayload || typeof optionsPayload !== "object") {
            throw new Error(contextLabel + " returned an invalid response.");
        }
        if (!optionsPayload.publicKey || typeof optionsPayload.publicKey !== "object") {
            throw new Error(contextLabel + " did not return valid credential options.");
        }
        if (typeof optionsPayload.flowToken !== "string" || !optionsPayload.flowToken) {
            throw new Error(contextLabel + " did not return a valid flow token.");
        }
        return optionsPayload;
    }

    function normalizeRequestOptions(publicKey) {
        const normalized = clonePublicKeyOptions(publicKey, "Passkey login");
        normalized.challenge = base64urlToBytes(normalized.challenge);
        normalized.allowCredentials = (normalized.allowCredentials || []).map(function (item) {
            return {
                ...item,
                id: base64urlToBytes(item.id),
            };
        });
        return normalized;
    }

    function normalizeCreationOptions(publicKey) {
        const normalized = clonePublicKeyOptions(publicKey, "Passkey registration");
        normalized.challenge = base64urlToBytes(normalized.challenge);
        normalized.user.id = base64urlToBytes(normalized.user.id);
        normalized.excludeCredentials = (normalized.excludeCredentials || []).map(function (item) {
            return {
                ...item,
                id: base64urlToBytes(item.id),
            };
        });
        return normalized;
    }

    function authenticationCredentialToJSON(credential) {
        return {
            id: credential.id,
            type: credential.type,
            rawId: bytesToBase64url(credential.rawId),
            response: {
                authenticatorData: bytesToBase64url(credential.response.authenticatorData),
                clientDataJSON: bytesToBase64url(credential.response.clientDataJSON),
                signature: bytesToBase64url(credential.response.signature),
                userHandle: credential.response.userHandle ? bytesToBase64url(credential.response.userHandle) : "",
            },
        };
    }

    function registrationCredentialToJSON(credential) {
        return {
            id: credential.id,
            type: credential.type,
            rawId: bytesToBase64url(credential.rawId),
            response: {
                attestationObject: bytesToBase64url(credential.response.attestationObject),
                clientDataJSON: bytesToBase64url(credential.response.clientDataJSON),
                transports: typeof credential.response.getTransports === "function" ? credential.response.getTransports() : [],
            },
        };
    }

    function secureContextMessage() {
        return "Passkeys require HTTPS or localhost/127.0.0.1 in the browser. Open the dev app on localhost or use HTTPS.";
    }

    function passkeysUnavailableMessage() {
        if (!window.PublicKeyCredential) {
            return "This browser does not support passkeys.";
        }
        if (!window.isSecureContext) {
            return secureContextMessage();
        }
        return "";
    }

    async function postJson(url, payload) {
        const response = await fetch(url, {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: payload === undefined ? undefined : JSON.stringify(payload),
        });
        const responseText = await response.text();
        let data = {};
        if (responseText) {
            try {
                data = JSON.parse(responseText);
            } catch (_error) {
                throw new Error("The server returned an unexpected response.");
            }
        }
        if (!response.ok) {
            throw new Error(data.detail || "Passkey request failed.");
        }
        return data;
    }

    function initPasskeyLogin() {
        const button = document.getElementById("passkey_login_button");
        const status = document.getElementById("passkey_login_status");
        const identifierInput = document.getElementById("username_or_email");
        if (!button || !status) {
            return;
        }

        const unavailableMessage = passkeysUnavailableMessage();
        if (unavailableMessage) {
            button.disabled = true;
            status.textContent = unavailableMessage;
            return;
        }

        button.addEventListener("click", async function () {
            const identifier = String(identifierInput && identifierInput.value || "").trim();
            status.textContent = "Preparing passkey login...";
            try {
                const optionsPayload = requireFlowPayload(
                    await postJson("/login/passkey/options", { identifier: identifier }),
                    "Passkey login",
                );
                if (!optionsPayload) {
                    return;
                }
                const assertion = await navigator.credentials.get({
                    publicKey: normalizeRequestOptions(optionsPayload.publicKey),
                });
                const verifyPayload = await postJson("/login/passkey/verify", {
                    flowToken: optionsPayload.flowToken,
                    credential: authenticationCredentialToJSON(assertion),
                });
                window.location.href = verifyPayload.redirectUrl || "/dashboard";
            } catch (error) {
                status.textContent = error instanceof Error ? error.message : "Passkey login failed.";
            }
        });
    }

    function initPasskeyRegistration() {
        const button = document.getElementById("passkey_register_button");
        const status = document.getElementById("passkey_register_status");
        const labelInput = document.getElementById("passkey_label");
        const passwordInput = document.getElementById("passkey_password");
        if (!button || !status || !passwordInput) {
            return;
        }

        const unavailableMessage = passkeysUnavailableMessage();
        if (unavailableMessage) {
            button.disabled = true;
            status.textContent = unavailableMessage;
            return;
        }

        button.addEventListener("click", async function () {
            const label = String(labelInput && labelInput.value || "Passkey");
            const currentPassword = String(passwordInput.value || "");
            status.textContent = "Preparing passkey registration...";
            try {
                const optionsPayload = requireFlowPayload(
                    await postJson("/account/security/passkeys/register/options", {
                        label: label,
                        currentPassword: currentPassword,
                    }),
                    "Passkey registration",
                );
                if (!optionsPayload) {
                    return;
                }
                const credential = await navigator.credentials.create({
                    publicKey: normalizeCreationOptions(optionsPayload.publicKey),
                });
                await postJson("/account/security/passkeys/register/verify", {
                    flowToken: optionsPayload.flowToken,
                    credential: registrationCredentialToJSON(credential),
                });
                window.location.reload();
            } catch (error) {
                status.textContent = error instanceof Error ? error.message : "Passkey registration failed.";
            }
        });
    }

    function initPasskeyMfa() {
        const button = document.getElementById("mfa_passkey_button");
        const status = document.getElementById("mfa_passkey_status");
        if (!button || !status) {
            return;
        }

        const unavailableMessage = passkeysUnavailableMessage();
        if (unavailableMessage) {
            button.disabled = true;
            status.textContent = unavailableMessage;
            return;
        }

        button.addEventListener("click", async function () {
            status.textContent = "Preparing passkey verification...";
            try {
                const optionsPayload = requireFlowPayload(
                    await postJson("/mfa/passkey/options"),
                    "Passkey verification",
                );
                if (!optionsPayload) {
                    return;
                }
                const assertion = await navigator.credentials.get({
                    publicKey: normalizeRequestOptions(optionsPayload.publicKey),
                });
                const verifyPayload = await postJson("/mfa/passkey/verify", {
                    flowToken: optionsPayload.flowToken,
                    credential: authenticationCredentialToJSON(assertion),
                });
                window.location.href = verifyPayload.redirectUrl || "/dashboard";
            } catch (error) {
                status.textContent = error instanceof Error ? error.message : "Passkey verification failed.";
            }
        });
    }

    function initAccountConfirmPasskeyAuth() {
        const button = document.getElementById("account_confirm_passkey_button");
        const status = document.getElementById("account_confirm_passkey_status");
        if (!button || !status) {
            return;
        }

        const unavailableMessage = passkeysUnavailableMessage();
        if (unavailableMessage) {
            button.disabled = true;
            status.textContent = unavailableMessage;
            return;
        }

        button.addEventListener("click", async function () {
            status.textContent = "Preparing passkey confirmation...";
            try {
                const optionsPayload = requireFlowPayload(
                    await postJson("/account/confirm/passkey/options"),
                    "Passkey confirmation",
                );
                if (!optionsPayload) {
                    return;
                }
                const assertion = await navigator.credentials.get({
                    publicKey: normalizeRequestOptions(optionsPayload.publicKey),
                });
                const verifyPayload = await postJson("/account/confirm/passkey/verify", {
                    flowToken: optionsPayload.flowToken,
                    credential: authenticationCredentialToJSON(assertion),
                });
                window.location.href = verifyPayload.redirectUrl || "/account/security";
            } catch (error) {
                status.textContent = error instanceof Error ? error.message : "Passkey confirmation failed.";
            }
        });
    }

    function initAccountConfirmPasskeyRegistration() {
        const button = document.getElementById("account_confirm_register_passkey_button");
        const status = document.getElementById("account_confirm_register_passkey_status");
        if (!button || !status) {
            return;
        }

        const unavailableMessage = passkeysUnavailableMessage();
        if (unavailableMessage) {
            button.disabled = true;
            status.textContent = unavailableMessage;
            return;
        }

        button.addEventListener("click", async function () {
            status.textContent = "Preparing passkey registration...";
            try {
                const optionsPayload = requireFlowPayload(
                    await postJson("/account/confirm/passkey/register/options"),
                    "Passkey registration",
                );
                if (!optionsPayload) {
                    return;
                }
                const credential = await navigator.credentials.create({
                    publicKey: normalizeCreationOptions(optionsPayload.publicKey),
                });
                const verifyPayload = await postJson("/account/confirm/passkey/register/verify", {
                    flowToken: optionsPayload.flowToken,
                    credential: registrationCredentialToJSON(credential),
                });
                window.location.href = verifyPayload.redirectUrl || "/account/security";
            } catch (error) {
                status.textContent = error instanceof Error ? error.message : "Passkey registration failed.";
            }
        });
    }

    document.addEventListener("DOMContentLoaded", function () {
        initPasskeyLogin();
        initPasskeyRegistration();
        initPasskeyMfa();
        initAccountConfirmPasskeyAuth();
        initAccountConfirmPasskeyRegistration();
    });
}());
