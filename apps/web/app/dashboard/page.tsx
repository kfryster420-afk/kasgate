import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { DashboardClient } from "@/components/dashboard-client";
import { readApiKey } from "@/lib/server/session";

export const metadata: Metadata = { title: "Dashboard" };
export const dynamic = "force-dynamic";

export default async function DashboardPage() {
  const apiKey = await readApiKey();
  if (!apiKey) redirect("/login");
  return <DashboardClient />;
}
