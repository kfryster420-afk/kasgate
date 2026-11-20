import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { DashboardClient } from "@/components/dashboard-client";
import { readSessionToken } from "@/lib/server/session";

export const metadata: Metadata = { title: "Dashboard" };
export const dynamic = "force-dynamic";

export default async function DashboardPage() {
  const token = await readSessionToken();
  if (!token) redirect("/login");
  return <DashboardClient />;
}
