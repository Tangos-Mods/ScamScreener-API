import { randomUUID } from "node:crypto";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import request from "supertest";

import { buildServer } from "../src/server";
import { createTempDatabase, seedInviteCode, type TempDbContext } from "./test-helpers";

describe("POST /api/v1/client/redeem", () => {
  let dbContext: TempDbContext;

  beforeEach(() => {
    dbContext = createTempDatabase("redeem-test");
  });

  afterEach(() => {
    dbContext.cleanup();
  });

  async function createApp() {
    const app = await buildServer({
      db: dbContext.db,
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

  it("redeems a valid invite and returns client credentials", async () => {
    seedInviteCode(dbContext.db, "invite-valid");
    const app = await createApp();

    const response = await request(app.server)
      .post("/api/v1/client/redeem")
      .send({
        inviteCode: "invite-valid",
        installId: randomUUID(),
        modVersion: "1.0.0"
      });

    expect(response.status).toBe(200);
    expect(response.body.ok).toBe(true);
    expect(response.body.signatureVersion).toBe("v1");
    expect(response.body.clientId).toMatch(/^relay-client-/);
    expect(response.body.clientSecret).toMatch(/^relay-secret-/);

    await app.close();
  });

  it("returns INVITE_INVALID for unknown invite", async () => {
    const app = await createApp();

    const response = await request(app.server)
      .post("/api/v1/client/redeem")
      .send({
        inviteCode: "missing",
        installId: randomUUID(),
        modVersion: "1.0.0"
      });

    expect(response.status).toBe(400);
    expect(response.body).toEqual({ ok: false, errorCode: "INVITE_INVALID" });

    await app.close();
  });

  it("returns INVITE_EXPIRED for expired invite", async () => {
    seedInviteCode(dbContext.db, "invite-expired", {
      expiresAt: "2000-01-01T00:00:00.000Z"
    });
    const app = await createApp();

    const response = await request(app.server)
      .post("/api/v1/client/redeem")
      .send({
        inviteCode: "invite-expired",
        installId: randomUUID(),
        modVersion: "1.0.0"
      });

    expect(response.status).toBe(410);
    expect(response.body).toEqual({ ok: false, errorCode: "INVITE_EXPIRED" });

    await app.close();
  });

  it("returns INVITE_ALREADY_USED when max uses reached", async () => {
    seedInviteCode(dbContext.db, "invite-used", {
      maxUses: 1,
      usedCount: 1
    });
    const app = await createApp();

    const response = await request(app.server)
      .post("/api/v1/client/redeem")
      .send({
        inviteCode: "invite-used",
        installId: randomUUID(),
        modVersion: "1.0.0"
      });

    expect(response.status).toBe(409);
    expect(response.body).toEqual({
      ok: false,
      errorCode: "INVITE_ALREADY_USED"
    });

    await app.close();
  });
});
