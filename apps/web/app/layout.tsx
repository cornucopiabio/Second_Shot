import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Second Shot",
  description: "Indication-first drug repurposing MVP",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
