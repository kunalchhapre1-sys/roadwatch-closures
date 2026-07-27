import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "RoadWatch — Active Road Closures",
  description:
    "A live OpenStreetMap dashboard for viewing and publishing active road closure GeoPackages.",
  openGraph: {
    title: "RoadWatch — Active Road Closures",
    description:
      "View and publish active road closures on a live OpenStreetMap dashboard.",
    type: "website",
  },
  twitter: {
    card: "summary",
    title: "RoadWatch — Active Road Closures",
    description:
      "View and publish active road closures on a live OpenStreetMap dashboard.",
  },
  icons: {
    icon: "/favicon.svg",
    shortcut: "/favicon.svg",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased`}
      >
        {children}
      </body>
    </html>
  );
}
