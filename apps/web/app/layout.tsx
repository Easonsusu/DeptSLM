import type { Metadata } from "next";
import type { ReactNode } from "react";

import "./globals.css";

export const metadata: Metadata = {
  title: "DeptSLM | Department AI, on your terms",
  description:
    "DeptSLM is a local prototype for department-scoped source, retrieval, evaluation, and adapter-governance boundaries.",
};

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
