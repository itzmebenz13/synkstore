// Create a Stripe Checkout Session and return the redirect URL.
// Set STRIPE_SECRET_KEY in Supabase Edge Function secrets.
// Runs in Supabase Edge Runtime (Deno).

import "jsr:@supabase/functions-js/edge-runtime.d.ts";
// @ts-ignore - ESM URL import (Deno); valid at deploy time
import Stripe from "https://esm.sh/stripe@14.21.0?target=deno";

declare const Deno: { env: { get(k: string): string | undefined }; serve: (h: (r: Request) => Response | Promise<Response>) => void };

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders });
  }

  const stripeSecret = Deno.env.get("STRIPE_SECRET_KEY");
  if (!stripeSecret) {
    return new Response(
      JSON.stringify({ message: "Stripe is not configured" }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );
  }

  try {
    const body = await req.json();
    const {
      product_title,
      quantity,
      total_php,
      user_id,
      credits_used,
      success_url,
      cancel_url,
    } = body;

    if (!product_title || quantity == null || total_php == null || !success_url || !cancel_url) {
      return new Response(
        JSON.stringify({ message: "Missing required fields: product_title, quantity, total_php, success_url, cancel_url" }),
        { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    const amount = Math.round(Number(total_php));
    const qty = Math.max(1, Math.round(Number(quantity)));
    if (amount <= 0) {
      return new Response(
        JSON.stringify({ message: "total_php must be positive" }),
        { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    const stripe = new Stripe(stripeSecret, { apiVersion: "2024-11-20.acacia" });
    // Stripe expects PHP in centavos (smallest unit): 18 pesos = 1800
    const amountCentavos = Math.round(amount * 100);
    const unitAmountCentavos = Math.max(100, Math.floor(amountCentavos / qty));
    const adjustedTotalCentavos = unitAmountCentavos * qty;
    const adjustedTotalPesos = adjustedTotalCentavos / 100;

    const session = await stripe.checkout.sessions.create({
      mode: "payment",
      success_url,
      cancel_url,
      line_items: [
        {
          price_data: {
            currency: "php",
            product_data: {
              name: String(product_title),
              description: qty > 1 ? `Quantity: ${qty}` : undefined,
            },
            unit_amount: unitAmountCentavos,
          },
          quantity: qty,
        },
      ],
      metadata: {
        product_title: String(product_title),
        quantity: String(qty),
        total_php: String(adjustedTotalPesos),
        user_id: user_id ? String(user_id) : "",
        credits_used: credits_used != null ? String(credits_used) : "0",
      },
    });

    return new Response(
      JSON.stringify({ url: session.url }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );
  } catch (err) {
    console.error(err);
    const message = err instanceof Error ? err.message : "Failed to create checkout session";
    return new Response(
      JSON.stringify({ message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );
  }
});
