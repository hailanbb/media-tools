import test from "node:test";
import assert from "node:assert/strict";
import { handleRequest } from "../src/index.js";

function withMockFetch(handler, fn) {
  const orig = globalThis.fetch;
  globalThis.fetch = handler;
  return Promise.resolve()
    .then(fn)
    .finally(() => {
      globalThis.fetch = orig;
    });
}

test("GET /health returns public status without secrets", async () => {
  const res = await handleRequest(
    new Request("https://douban.ubaby.top/health"),
    { PROXY_TOKEN: "secret123", DOUBAN_COOKIE: "dbcl2=\"1:abc\"" }
  );
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.equal(body.ok, true);
  assert.equal(body.service, "mediaflow-douban-proxy");
  assert.equal(body.auth_required, true);
  assert.equal(body.has_server_cookie, true);
  assert.equal(JSON.stringify(body).includes("secret123"), false);
  assert.equal(JSON.stringify(body).includes("dbcl2"), false);
});

test("enforces PROXY_TOKEN via X-Proxy-Token or Bearer", async () => {
  const env = { PROXY_TOKEN: "top-secret-token" };
  const unauth = await handleRequest(
    new Request("https://douban.ubaby.top/suggest?q=%E5%BA%86%E4%BD%99%E5%B9%B4"),
    env
  );
  assert.equal(unauth.status, 401);

  const wrong = await handleRequest(
    new Request("https://douban.ubaby.top/suggest?q=%E5%BA%86%E4%BD%99%E5%B9%B4", {
      headers: { "X-Proxy-Token": "wrong-token" },
    }),
    env
  );
  assert.equal(wrong.status, 401);

  await withMockFetch(
    async () =>
      new Response(JSON.stringify([{ id: "25853071", title: "庆余年" }]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    async () => {
      const okBearer = await handleRequest(
        new Request("https://douban.ubaby.top/suggest?q=%E5%BA%86%E4%BD%99%E5%B9%B4", {
          headers: { Authorization: "Bearer top-secret-token" },
        }),
        env
      );
      assert.equal(okBearer.status, 200);
      assert.equal(okBearer.headers.get("X-Douban-Upstream-Channel"), "suggest");
      const data = await okBearer.json();
      assert.equal(data[0].title, "庆余年");
    }
  );
});

test("rejects non-GET methods and unknown paths (SSRF guard)", async () => {
  const postRes = await handleRequest(
    new Request("https://douban.ubaby.top/suggest?q=test", { method: "POST" }),
    {}
  );
  assert.equal(postRes.status, 405);

  const badPath = await handleRequest(
    new Request("https://douban.ubaby.top/proxy?url=http://169.254.169.254/"),
    {}
  );
  assert.equal(badPath.status, 404);

  const missingQ = await handleRequest(
    new Request("https://douban.ubaby.top/suggest"),
    {}
  );
  assert.equal(missingQ.status, 400);
});

test("forwards X-Douban-Cookie and auto-generates 11-char bid if missing", async () => {
  let capturedHeaders = null;
  let capturedUrl = null;
  await withMockFetch(
    async (url, init) => {
      capturedUrl = url;
      capturedHeaders = init.headers;
      return new Response(JSON.stringify({ id: "25853071", title: "庆余年", episodes_count: 46 }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    },
    async () => {
      const res = await handleRequest(
        new Request("https://douban.ubaby.top/rexxar/tv/25853071", {
          headers: { "X-Douban-Cookie": 'dbcl2="12345:xyz"' },
        }),
        {}
      );
      assert.equal(res.status, 200);
      assert.equal(capturedUrl, "https://m.douban.com/rexxar/api/v2/tv/25853071");
      assert.match(capturedHeaders.Cookie, /dbcl2="12345:xyz"/);
      assert.match(capturedHeaders.Cookie, /bid=[A-Za-z0-9]{11}/);
      assert.equal(capturedHeaders.Referer, "https://m.douban.com/movie/subject/25853071/");
    }
  );
});

test("frodo route rotates public keys on 500 and stops immediately on 403", async () => {
  const urls = [];
  await withMockFetch(
    async (url) => {
      urls.push(url);
      if (urls.length === 1) {
        return new Response("upstream err", { status: 500 });
      }
      return new Response(JSON.stringify({ title: "庆余年" }), { status: 200 });
    },
    async () => {
      const res = await handleRequest(new Request("https://douban.ubaby.top/frodo/tv/25853071"), {});
      assert.equal(res.status, 200);
      assert.equal(urls.length, 2);
    }
  );

  const forbiddenUrls = [];
  await withMockFetch(
    async (url) => {
      forbiddenUrls.push(url);
      return new Response("error code: 004", { status: 403 });
    },
    async () => {
      const res = await handleRequest(new Request("https://douban.ubaby.top/frodo/tv/25853071"), {});
      assert.equal(res.status, 403);
      assert.equal(forbiddenUrls.length, 1);
    }
  );
});

test("subject route proxies douban movie subject HTML", async () => {
  await withMockFetch(
    async (url) => {
      assert.equal(url, "https://movie.douban.com/subject/25853071/");
      return new Response("<html><span property='v:itemreviewed'>庆余年</span></html>", {
        status: 200,
        headers: { "Content-Type": "text/html; charset=utf-8" },
      });
    },
    async () => {
      const res = await handleRequest(new Request("https://douban.ubaby.top/subject/25853071"), {});
      assert.equal(res.status, 200);
      assert.equal(res.headers.get("X-Douban-Upstream-Channel"), "subject");
      const html = await res.text();
      assert.match(html, /庆余年/);
    }
  );
});

test("probe aggregates suggest, rexxar_tv, and frodo_tv checks", async () => {
  await withMockFetch(
    async (url) => {
      if (url.includes("subject_suggest")) {
        return new Response(JSON.stringify([{ id: "25853071", title: "庆余年" }]), { status: 200 });
      }
      if (url.includes("rexxar")) {
        return new Response(JSON.stringify({ id: "25853071", title: "庆余年" }), { status: 200 });
      }
      return new Response("error code: 004", { status: 403 });
    },
    async () => {
      const res = await handleRequest(new Request("https://douban.ubaby.top/probe"), {});
      assert.equal(res.status, 200);
      const body = await res.json();
      assert.equal(body.ok, true);
      assert.equal(body.results.suggest.ok, true);
      assert.equal(body.results.suggest.sample_title, "庆余年");
      assert.equal(body.results.rexxar_tv.ok, true);
      assert.equal(body.results.frodo_tv.ok, false);
      assert.equal(body.results.frodo_tv.http_status, 403);
    }
  );
});
