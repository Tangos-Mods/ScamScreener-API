import { randomUUID } from "node:crypto";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import request from "supertest";

import type { DiscordForwarder } from "../src/discord/forward";
import { buildCanonicalString, hmacSha256Hex, sha256Hex } from "../src/security/signature";
import { buildServer } from "../src/server";
import { createTempDatabase, seedClient, type TempDbContext } from "./test-helpers";

describe("POST /api/v1/training-uploads", () => {
  let dbContext: TempDbContext;

  beforeEach(() => {
    dbContext = createTempDatabase("upload-test");
  });

  afterEach(() => {
    dbContext.cleanup();
  });

  async function createApp(forwarder: DiscordForwarder) {
    const app = await buildServer({
      db: dbContext.db,
      discordForwarder: forwarder,
      config: {
        port: 8080,
        nodeEnv: "test",
        discordWebhookUrl: "https://example.com/webhook",
        maxUploadBytes: 26_214_400,
        maxClockSkewSeconds: 300,
        nonceTtlSeconds: 600,
        rateLimitPerClientPerMinute: 30,
        rateLimitRedeemPerIpPerMinute: 60,
        logLevel: "silent",
        sqlitePath: dbContext.dbPath
      }
    });

    await app.ready();
    return app;
  }

  function buildSignedHeaders(
    clientId: string,
    clientSecret: string,
    nonce: string,
    metadata: {
      fileSha256: string;
      fileSizeBytes: number;
      schemaVersion: "1";
    }
  ) {
    const timestamp = Math.floor(Date.now() / 1000).toString();
    const canonical = buildCanonicalString({
      method: "POST",
      path: "/api/v1/training-uploads",
      clientId,
      timestamp,
      nonce,
      fileSha256: metadata.fileSha256,
      fileSizeBytes: metadata.fileSizeBytes,
      schemaVersion: metadata.schemaVersion
    });
    const signature = hmacSha256Hex(clientSecret, canonical);

    return {
      "X-ScamScreener-Client-Id": clientId,
      "X-ScamScreener-Timestamp": timestamp,
      "X-ScamScreener-Nonce": nonce,
      "X-ScamScreener-Signature": signature,
      "X-ScamScreener-Signature-Version": "v1"
    };
  }

  it("accepts valid upload and forwards to Discord", async () => {
    const clientId = "relay-client-test";
    const clientSecret = "relay-secret-test";
    seedClient(dbContext.db, clientId, clientSecret);

    const discordForwarder = vi.fn(async () => ({ messageId: "1234567890" }));
    const app = await createApp(discordForwarder);

    const csv = Buffer.from("label,score\nscam,0.95\n", "utf8");
    const metadata = {
      schemaVersion: "1" as const,
      modVersion: "1.0.0",
      aiModelVersion: "model-1",
      playerName: "Player1",
      playerUuid: randomUUID(),
      clientTimestamp: new Date().toISOString(),
      fileSha256: sha256Hex(csv),
      fileSizeBytes: csv.length
    };

    const response = await request(app.server)
      .post("/api/v1/training-uploads")
      .set(buildSignedHeaders(clientId, clientSecret, randomUUID(), metadata))
      .field("metadata", JSON.stringify(metadata))
      .attach("training_file", csv, {
        filename: "training-data.csv",
        contentType: "text/csv"
      });

    expect(response.status).toBe(200);
    expect(response.body.ok).toBe(true);
    expect(response.body.discordMessageId).toBe("1234567890");
    expect(response.body.verifiedFileSha256).toBe(metadata.fileSha256);
    expect(discordForwarder).toHaveBeenCalledTimes(1);

    await app.close();
  });

  it("rejects invalid signatures", async () => {
    const clientId = "relay-client-test";
    const clientSecret = "relay-secret-test";
    seedClient(dbContext.db, clientId, clientSecret);

    const discordForwarder = vi.fn(async () => ({ messageId: "1234567890" }));
    const app = await createApp(discordForwarder);

    const csv = Buffer.from("label,score\nscam,0.95\n", "utf8");
    const metadata = {
      schemaVersion: "1" as const,
      modVersion: "1.0.0",
      aiModelVersion: "model-1",
      playerName: "Player1",
      playerUuid: randomUUID(),
      clientTimestamp: new Date().toISOString(),
      fileSha256: sha256Hex(csv),
      fileSizeBytes: csv.length
    };

    const headers = buildSignedHeaders(clientId, clientSecret, randomUUID(), metadata);
    headers["X-ScamScreener-Signature"] = "0".repeat(64);

    const response = await request(app.server)
      .post("/api/v1/training-uploads")
      .set(headers)
      .field("metadata", JSON.stringify(metadata))
      .attach("training_file", csv, {
        filename: "training-data.csv",
        contentType: "text/csv"
      });

    expect(response.status).toBe(401);
    expect(response.body.ok).toBe(false);
    expect(response.body.errorCode).toBe("AUTH_FAILED");
    expect(discordForwarder).not.toHaveBeenCalled();

    await app.close();
  });

  it("rejects nonce replay", async () => {
    const clientId = "relay-client-test";
    const clientSecret = "relay-secret-test";
    seedClient(dbContext.db, clientId, clientSecret);

    const discordForwarder = vi.fn(async () => ({ messageId: "1234567890" }));
    const app = await createApp(discordForwarder);

    const csv = Buffer.from("label,score\nscam,0.95\n", "utf8");
    const metadata = {
      schemaVersion: "1" as const,
      modVersion: "1.0.0",
      aiModelVersion: "model-1",
      playerName: "Player1",
      playerUuid: randomUUID(),
      clientTimestamp: new Date().toISOString(),
      fileSha256: sha256Hex(csv),
      fileSizeBytes: csv.length
    };
    const nonce = randomUUID();
    const headers = buildSignedHeaders(clientId, clientSecret, nonce, metadata);

    const firstResponse = await request(app.server)
      .post("/api/v1/training-uploads")
      .set(headers)
      .field("metadata", JSON.stringify(metadata))
      .attach("training_file", csv, {
        filename: "training-data.csv",
        contentType: "text/csv"
      });
    expect(firstResponse.status).toBe(200);

    const secondResponse = await request(app.server)
      .post("/api/v1/training-uploads")
      .set(headers)
      .field("metadata", JSON.stringify(metadata))
      .attach("training_file", csv, {
        filename: "training-data.csv",
        contentType: "text/csv"
      });

    expect(secondResponse.status).toBe(409);
    expect(secondResponse.body.errorCode).toBe("NONCE_REPLAY");

    await app.close();
  });

  it("rejects hash mismatch", async () => {
    const clientId = "relay-client-test";
    const clientSecret = "relay-secret-test";
    seedClient(dbContext.db, clientId, clientSecret);

    const discordForwarder = vi.fn(async () => ({ messageId: "1234567890" }));
    const app = await createApp(discordForwarder);

    const csv = Buffer.from("label,score\nscam,0.95\n", "utf8");
    const metadata = {
      schemaVersion: "1" as const,
      modVersion: "1.0.0",
      aiModelVersion: "model-1",
      playerName: "Player1",
      playerUuid: randomUUID(),
      clientTimestamp: new Date().toISOString(),
      fileSha256: "f".repeat(64),
      fileSizeBytes: csv.length
    };

    const response = await request(app.server)
      .post("/api/v1/training-uploads")
      .set(buildSignedHeaders(clientId, clientSecret, randomUUID(), metadata))
      .field("metadata", JSON.stringify(metadata))
      .attach("training_file", csv, {
        filename: "training-data.csv",
        contentType: "text/csv"
      });

    expect(response.status).toBe(422);
    expect(response.body.errorCode).toBe("PAYLOAD_INVALID");
    expect(discordForwarder).not.toHaveBeenCalled();

    await app.close();
  });

  it("returns DISCORD_FORWARD_FAILED if webhook forwarding fails", async () => {
    const clientId = "relay-client-test";
    const clientSecret = "relay-secret-test";
    seedClient(dbContext.db, clientId, clientSecret);

    const discordForwarder = vi.fn(async () => {
      throw new Error("downstream failed");
    });
    const app = await createApp(discordForwarder);

    const csv = Buffer.from("label,score\nscam,0.95\n", "utf8");
    const metadata = {
      schemaVersion: "1" as const,
      modVersion: "1.0.0",
      aiModelVersion: "model-1",
      playerName: "Player1",
      playerUuid: randomUUID(),
      clientTimestamp: new Date().toISOString(),
      fileSha256: sha256Hex(csv),
      fileSizeBytes: csv.length
    };

    const response = await request(app.server)
      .post("/api/v1/training-uploads")
      .set(buildSignedHeaders(clientId, clientSecret, randomUUID(), metadata))
      .field("metadata", JSON.stringify(metadata))
      .attach("training_file", csv, {
        filename: "training-data.csv",
        contentType: "text/csv"
      });

    expect(response.status).toBe(502);
    expect(response.body.errorCode).toBe("DISCORD_FORWARD_FAILED");

    await app.close();
  });
});
