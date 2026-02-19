import type { UploadMetadata } from "../types/api";

export interface ForwardUploadInput {
  requestId: string;
  metadata: UploadMetadata;
  csvData: Buffer;
  verifiedFileSha256: string;
}

export interface DiscordForwardResult {
  messageId: string | null;
}

export type DiscordForwarder = (
  webhookUrl: string,
  input: ForwardUploadInput
) => Promise<DiscordForwardResult>;

export const forwardUploadToDiscord: DiscordForwarder = async (
  webhookUrl,
  input
) => {
  const url = webhookUrl.includes("?")
    ? `${webhookUrl}&wait=true`
    : `${webhookUrl}?wait=true`;

  const embed = {
    title: "ScamScreener Upload",
    fields: [
      { name: "Mod Version", value: input.metadata.modVersion, inline: true },
      {
        name: "AI Model Version",
        value: input.metadata.aiModelVersion,
        inline: true
      },
      { name: "Player UUID", value: input.metadata.playerUuid, inline: false },
      { name: "CSV SHA-256", value: input.verifiedFileSha256, inline: false }
    ],
    footer: { text: `Request ${input.requestId}` },
    timestamp: new Date().toISOString()
  };

  const formData = new FormData();
  formData.set(
    "payload_json",
    JSON.stringify({
      embeds: [embed]
    })
  );
  formData.set(
    "files[0]",
    new Blob([input.csvData.toString("utf8")], { type: "text/csv" }),
    "training-data.csv"
  );

  const response = await fetch(url, {
    method: "POST",
    body: formData
  });

  if (!response.ok) {
    const errorText = await response.text();
    throw new Error(
      `Discord forward failed (${response.status}): ${errorText.slice(0, 256)}`
    );
  }

  const responseJson = (await response.json().catch(() => null)) as
    | { id?: string }
    | null;

  return {
    messageId:
      responseJson && typeof responseJson.id === "string"
        ? responseJson.id
        : null
  };
};
