/** @type {import('next').NextConfig} */
export function resolveApiUrl(raw = process.env.API_URL) {
  const value = (raw ?? "http://api:8000").trim();
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error("API_URL must be an absolute HTTP(S) URL.");
  }
  if (
    !["http:", "https:"].includes(parsed.protocol) ||
    !parsed.hostname ||
    parsed.username ||
    parsed.password ||
    parsed.search ||
    parsed.hash
  ) {
    throw new Error("API_URL must be an absolute HTTP(S) URL without credentials or query data.");
  }
  return `${parsed.origin}${parsed.pathname.replace(/\/+$/, "")}`;
}

const nextConfig = {
  output: "standalone",
  poweredByHeader: false,
  reactStrictMode: true,
  async rewrites() {
    // The host is deployment configuration, never request data. Only the
    // matched path suffix is forwarded to this fixed server-side destination.
    const apiUrl = resolveApiUrl();
    return [{ source: "/api/:path*", destination: `${apiUrl}/:path*` }];
  },
};

export default nextConfig;
