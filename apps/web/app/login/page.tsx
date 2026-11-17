import type { Metadata } from "next";
import Link from "next/link";
import { LoginForm } from "@/components/login-form";

export const metadata: Metadata = { title: "Sign in" };

export default function LoginPage() {
  return (
    <div className="mx-auto max-w-md px-4 py-16">
      <h1 className="text-2xl font-semibold text-zinc-100">Sign in to your dashboard</h1>
      <p className="mb-6 mt-1 text-sm text-zinc-400">
        Sign a one-time message with your payout wallet to get in. New here?{" "}
        <Link href="/onboarding" className="text-kas-400 hover:underline">
          Start onboarding
        </Link>
        .
      </p>
      <LoginForm />
    </div>
  );
}
