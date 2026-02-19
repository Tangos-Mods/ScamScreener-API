import multipart from "@fastify/multipart";
import rateLimit from "@fastify/rate-limit";
import type Database from "better-sqlite3";
import Fastify, { type FastifyInstance } from "fastify";

import { type AppConfig, loadConfig, resolveConfig } from "./config";
import { initDatabase } from "./db";
import {
  forwardUploadToDiscord,
  type DiscordForwarder
} from "./discord/forward";
import { registerRedeemRoute } from "./routes/redeem";
import { registerUploadRoute } from "./routes/upload";
import { cleanupExpiredNonces } from "./security/nonces";

interface BuildServerOptions {
  config?: Partial<AppConfig>;
  db?: Database.Database;
  discordForwarder?: DiscordForwarder;
}

export async function buildServer(
  options: BuildServerOptions = {}
): Promise<FastifyInstance> {
  const config = resolveConfig(options.config);
  const db = options.db ?? initDatabase(config.sqlitePath);
  const discordForwarder = options.discordForwarder ?? forwardUploadToDiscord;

  const app = Fastify({
    logger: {
      level: config.logLevel,
      transport:
        config.nodeEnv === "production"
          ? undefined
          : {
              target: "pino-pretty",
              options: { translateTime: "SYS:standard", ignore: "pid,hostname" }
            }
    },
    bodyLimit: config.maxUploadBytes
  });

  await app.register(multipart, {
    limits: {
      fileSize: config.maxUploadBytes,
      files: 1,
      fields: 8
    }
  });

  await app.register(rateLimit, {
    global: false,
    errorResponseBuilder: () => ({
      ok: false,
      errorCode: "RATE_LIMITED"
    })
  });

  await registerRedeemRoute(app, { db, config });
  await registerUploadRoute(app, { db, config, discordForwarder });

  app.get("/healthz", async () => ({ ok: true }));

  const cleanupEveryMs = Math.max(30, Math.floor(config.nonceTtlSeconds / 2)) * 1000;
  const cleanupTimer = setInterval(() => {
    try {
      cleanupExpiredNonces(db);
    } catch (error) {
      app.log.warn({ err: error }, "Failed periodic nonce cleanup");
    }
  }, cleanupEveryMs);
  cleanupTimer.unref();

  app.addHook("onClose", async () => {
    clearInterval(cleanupTimer);
    if (!options.db) {
      db.close();
    }
  });

  return app;
}

async function startServer(): Promise<void> {
  const config = loadConfig();
  const app = await buildServer({ config });

  await app.listen({
    host: "0.0.0.0",
    port: config.port
  });
}

if (require.main === module) {
  void startServer().catch((error) => {
    // eslint-disable-next-line no-console
    console.error(error);
    process.exit(1);
  });
}
