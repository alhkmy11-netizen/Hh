// src/lib/shopping-chat.server.ts
// منطق شات حكيم: كتالوج حي من Supabase + برومبت المستشار + سلسلة مزودين مجانيين
// Groq → Gemini → Cloudflare → Lovable (اختياري)
import { createOpenAICompatible } from "@ai-sdk/openai-compatible";
import {
  convertToModelMessages,
  generateText,
  streamText,
  type UIMessage,
} from "ai";
import { deals, stores } from "@/data/deals";

type CatalogLine = string;

async function loadLiveCatalog(): Promise<{ deals: CatalogLine[]; coupons: CatalogLine[] }> {
  try {
    const { supabaseAdmin } = await import("@/integrations/supabase/client.server");
    const nowIso = new Date().toISOString();
    const [merchantRes, externalRes, couponRes] = await Promise.all([
      supabaseAdmin
        .from("merchant_deals")
        .select(
          "id,title,unit,price,original_price,expires_at,coupon_code,product_url,merchants!inner(name)",
        )
        .eq("status", "published")
        .order("discount_percent", { ascending: false })
        .limit(60),
      supabaseAdmin
        .from("external_deals")
        .select("id,title,unit,price,original_price,store_name,store_id,expires_at,product_url")
        .eq("active", true)
        .order("discount_percent", { ascending: false })
        .limit(60),
      supabaseAdmin
        .from("coupons")
        .select("code,store_name,title,discount,expires_at")
        .eq("active", true)
        .limit(30),
    ]);

    const dealLines: CatalogLine[] = [];
    for (const row of merchantRes.data ?? []) {
      const isExpired = row.expires_at ? row.expires_at < nowIso : false;
      const merchant = (row as { merchants?: { name?: string } }).merchants;
      dealLines.push(
        `- [id:${row.id}] ${row.title}${row.unit ? ` (${row.unit})` : ""} | متجر: ${merchant?.name ?? "تاجر موثّق"} | سعر: ${row.price} ر.س | قبل: ${row.original_price} ر.س${row.coupon_code ? ` | كود: ${row.coupon_code}` : ""}${isExpired ? " | ملاحظة: انتهت صلاحية العرض" : ""}`,
      );
    }
    for (const row of externalRes.data ?? []) {
      dealLines.push(
        `- [id:${row.id}] ${row.title}${row.unit ? ` (${row.unit})` : ""} | متجر: ${row.store_name ?? row.store_id} | سعر: ${row.price} ر.س | قبل: ${row.original_price} ر.س`,
      );
    }

    const couponLines = (couponRes.data ?? []).map(
      (c) => `- ${c.store_name}: كود ${c.code} — ${c.title}${c.discount ? ` (${c.discount})` : ""}`,
    );

    return { deals: dealLines, coupons: couponLines };
  } catch {
    return { deals: [], coupons: [] };
  }
}

// ─── سلسلة المزودين (المفاتيح من البيئة فقط — لا مفاتيح في الكود) ───

type Provider = {
  name: string;
  model: string;
  client: () => ReturnType<typeof createOpenAICompatible>;
};

function buildProviders(): Provider[] {
  const list: Provider[] = [];

  if (process.env.GROQ_API_KEY) {
    const key = process.env.GROQ_API_KEY;
    list.push({
      name: "groq",
      model: "llama-3.3-70b-versatile",
      client: () =>
        createOpenAICompatible({
          name: "groq",
          baseURL: "https://api.groq.com/openai/v1",
          apiKey: key,
        }),
    });
  }

  if (process.env.GEMINI_API_KEY) {
    const key = process.env.GEMINI_API_KEY;
    list.push({
      name: "gemini",
      model: "gemini-2.5-flash",
      client: () =>
        createOpenAICompatible({
          name: "gemini",
          baseURL: "https://generativelanguage.googleapis.com/v1beta/openai",
          apiKey: key,
        }),
    });
  }

  if (process.env.CF_ACCOUNT_ID && process.env.CF_API_TOKEN) {
    const account = process.env.CF_ACCOUNT_ID;
    const key = process.env.CF_API_TOKEN;
    list.push({
      name: "cloudflare",
      model: "@cf/meta/llama-3.1-8b-instruct",
      client: () =>
        createOpenAICompatible({
          name: "cloudflare",
          baseURL: `https://api.cloudflare.com/client/v4/accounts/${account}/ai/v1`,
          apiKey: key,
        }),
    });
  }

  if (process.env.LOVABLE_API_KEY) {
    const key = process.env.LOVABLE_API_KEY;
    list.push({
      name: "lovable",
      model: "google/gemini-2.5-flash",
      // بوابة Lovable تستخدم ترويسة مخصصة (كما في ملف ai-gateway الأصلي عندك)
      client: () =>
        createOpenAICompatible({
          name: "lovable",
          baseURL: "https://ai.gateway.lovable.dev/v1",
          headers: {
            "Lovable-API-Key": key,
            "X-Lovable-AIG-SDK": "vercel-ai-sdk",
          },
        }),
    });
  }

  return list;
}

// فحص توفر رخيص قبل البث: إذا رفض مزود (نفاد حصة/مفتاح) ننتقل للتالي فورًا
async function pickAvailableProvider(): Promise<Provider | null> {
  for (const p of buildProviders()) {
    try {
      await generateText({ model: p.client()(p.model), system: "ok", prompt: "ping" });
      return p;
    } catch (err) {
      const status =
        (err as { status?: number })?.status ??
        (err as { statusCode?: number })?.statusCode;
      console.warn(`[hkeeem-ai] المزود ${p.name} غير متاح (${status ?? "خطأ"}) — الانتقال للتالي`);
      continue;
    }
  }
  return null;
}

// ─── نقطة الدخول التي يستدعيها chat.ts ───
export async function handleShoppingChat(request: Request): Promise<Response> {
  try {
    const { messages } = (await request.json()) as { messages?: UIMessage[] };
    if (!Array.isArray(messages)) return new Response("bad request", { status: 400 });
    if (messages.length > 40) return new Response("too many messages", { status: 400 });
    const totalChars = JSON.stringify(messages).length;
    if (totalChars > 24000) return new Response("conversation too long", { status: 400 });

    const live = await loadLiveCatalog();

    const staticLines = deals.map((d) => {
      const s = stores.find((x) => x.id === d.storeId);
      return `- [id:${d.id}] ${d.title}${d.unit ? ` (${d.unit})` : ""} | متجر: ${s?.name ?? "متجر"} | سعر: ${d.price} ر.س | قبل: ${d.originalPrice} ر.س | تنتهي: ${d.expiresIn}`;
    });

    const allLines = [...live.deals, ...staticLines];
    const catalog = allLines.length
      ? allLines.join("\n")
      : "(لا توجد عروض محدّثة في القاعدة الآن)";
    const couponsBlock = live.coupons.length
      ? live.coupons.join("\n")
      : "(لا توجد أكواد خصم مفعّلة الآن)";

    const system = `أنت "حكيم"، مساعد ذكي وودود متخصص في عروض المملكة. تكلّم باللغة العربية الفصحى المعاصرة فقط، بوضوح واختصار (سطران إلى ثلاثة كحد أقصى)، لأن ردودك قد تُقرأ بصوت عالٍ. عرّف نفسك كـ"حكيم" عندما يسألك المستخدم عن اسمك.

قاعدة العروض المتاحة اليوم:
${catalog}

أكواد الخصم المفعّلة:
${couponsBlock}



قواعد:
- استخدم فقط العروض أعلاه، لا تخترع أسعاراً أو متاجر.
- عند المقارنة، رتّب الأرخص أوّلاً واذكر نسبة التوفير.
- تجنّب الرموز التعبيرية والرموز الخاصة قدر الإمكان (الردود قد تُنطق صوتياً).
- ممنوع تماماً استخدام العامية أو كلمات مثل: "شلون"، "كيف حالك"، "هلا"، "يعطيك العافية"، "ما في"، "ما فيه"، "لا يوجد"، "ما لقيت"، "ما عندنا"، "غير متوفر"، "غير موجود". حتى لو القاعدة فارغة، لا تقلها.
- إذا لم تجد المنتج بالضبط: ابدأ بـ "عندي بديل أحسن لك" أو "جرب هذا" أو "استخدم كود الخصم" أو "فعّل تنبيه سعر"، وقدّم دائماً خياراً واحداً على الأقل: أقرب منتج مشابه من القاعدة، أو كود خصم مفعّل من قائمة الأكواد أعلاه، أو اقترح متابعة صفحة العروض/تفعيل تنبيه انخفاض السعر.
- إذا كانت القاعدة فارغة تماماً، قل "العروض تُحدَّث أولاً بأول" ثم قدّم كود خصم مفعّل أو تنبيه سعر، ولا تخترع أي سعراً.
- إذا كان العرض مكتوباً عليه "انتهت صلاحية العرض"، نبّه المستخدم أنه قد يكون منتهياً قبل أن توصي به.
- مهم جداً: كلما ذكرت عرضاً محدداً، ألصق بعده مباشرة الرمز {{deal:المعرّف}} حيث المعرّف هو قيمة id من القاعدة أعلاه، بدون فراغ. مثال: "أرز بسمتي من العثيم بـ 39 ر.س {{deal:othaim-basmati}}". لا تعرض المعرّف بأي شكل آخر ولا تنطقه.`;

    const provider = await pickAvailableProvider();
    if (!provider) {
      return new Response(
        JSON.stringify({ error: "مساعد حكيم غير متاح مؤقتًا، جرّب بعد قليل" }),
        { status: 503, headers: { "content-type": "application/json; charset=utf-8" } },
      );
    }

    const result = streamText({
      model: provider.client()(provider.model),
      system,
      messages: await convertToModelMessages(messages),
    });

    return result.toUIMessageStreamResponse({ originalMessages: messages });
  } catch (err) {
    console.error("[hkeeem-ai] خطأ غير متوقع في handleShoppingChat:", err);
    return new Response(
      JSON.stringify({ error: "تعذّر تنفيذ طلب المساعد، حاول لاحقًا" }),
      { status: 503, headers: { "content-type": "application/json; charset=utf-8" } },
    );
  }
}
