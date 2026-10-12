import Fastify from "fastify";
import { DynamoDBClient } from "@aws-sdk/client-dynamodb";
import {
  DynamoDBDocumentClient,
  ScanCommand,
  type ScanCommandInput,
} from "@aws-sdk/lib-dynamodb";

const app = Fastify({ logger: true });

const db = DynamoDBDocumentClient.from(
  new DynamoDBClient({
    region: process.env.AWS_REGION ?? "us-west-2",
  }),
);

app.get("/api/health", async () => {
  return { status: "ok" };
});

app.get("/api/risks", async (_request, reply) => {
  try {
    const risks: Record<string, unknown>[] = [];
    let cursor: ScanCommandInput["ExclusiveStartKey"];

    do {
      const page = await db.send(
        new ScanCommand({
          TableName: "supplier_risk_state",
          ExclusiveStartKey: cursor,
        }),
      );

      risks.push(...(page.Items ?? []));
      cursor = page.LastEvaluatedKey;
    } while (cursor);

    risks.sort((a, b) =>
      String(b.last_seen_at ?? "").localeCompare(
        String(a.last_seen_at ?? ""),
      ),
    );

    return { risks, count: risks.length };
  } catch (error) {
    app.log.error(error, "Failed to load risks");

    return reply.code(503).send({
      error: "Risk data is currently unavailable",
    });
  }
});

await app.listen({
  port: 3001,
  host: "127.0.0.1",
});