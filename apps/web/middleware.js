import { NextResponse } from "next/server";

import { developmentAuthorization } from "./lib/dev-auth.mjs";

export function middleware(request) {
  const existingAuthorization = request.headers.get("authorization");
  const authorization = developmentAuthorization({
    environment: process.env.ENVIRONMENT,
    token: process.env.DEPTSLM_WEB_DEV_BEARER_TOKEN,
    existingAuthorization,
  });

  if (!authorization || authorization === existingAuthorization) {
    return NextResponse.next();
  }

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("authorization", authorization);
  return NextResponse.next({ request: { headers: requestHeaders } });
}

export const config = {
  matcher: ["/api/:path*"],
};
