import { describe, expect, it } from "vitest";
import { QueryClient } from "@tanstack/react-query";
import Ajv2020 from "ajv/dist/2020";
import addFormats from "ajv-formats";
import schema from "../../../../../contracts/openapi.json";
import { createTransport } from "@/lib/api/client";
import { workloadsQueryOptions } from "./queries";
import { workloadFixture } from "./fixtures";

describe("workload consumer contract", () => {
  it("validates typed fixture data against the backend-owned schema", () => {
    const ajv = new Ajv2020({ strict: false });
    addFormats(ajv);
    ajv.addSchema({ $id: "cloudops", components: schema.components });
    const validate = ajv.compile({
      $ref: "cloudops#/components/schemas/WorkloadPage",
    });
    expect(validate(workloadFixture), JSON.stringify(validate.errors)).toBe(
      true,
    );
    expect(
      validate({
        ...workloadFixture,
        items: [{ ...workloadFixture.items[0], health: "invented-health" }],
      }),
    ).toBe(false);
  });
  it("uses the feature query unchanged against a typed HTTP fixture", async () => {
    const transport = createTransport({
      baseUrl: "http://localhost",
      fetch: async (request) => {
        expect(String((request as Request).url)).toContain(
          "/api/workloads?limit=25&offset=0",
        );
        return Response.json(workloadFixture);
      },
    });
    const cache = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    const data = await cache.fetchQuery(
      workloadsQueryOptions(0, transport.client),
    );
    expect(data.items[0].health).toBe("unknown");
    cache.clear();
  });
});
