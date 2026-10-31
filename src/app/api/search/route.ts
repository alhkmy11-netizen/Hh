import { NextResponse } from 'next/server';
import { createClient } from '@supabase/supabase-js';

// الاتصال بقاعدة البيانات
const supabase = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.SUPABASE_SERVICE_ROLE_KEY!
);

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const query = searchParams.get('q');

  if (!query) {
    return NextResponse.json([]);
  }

  // البحث عن المنتج (باستخدام دالة ilike للبحث المرن)
  const { data: products, error } = await supabase
    .from('products')
    .select('*, prices(*)')
    .ilike('name', `%${query}%`);

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  // معالجة البيانات لحساب أفضل سعر والأرخص
  const results = products.map((p: any) => {
    const offers = p.prices.sort((a: any, b: any) => (a.price + a.shipping) - (b.price + b.shipping));
    return {
      id: p.id,
      name: p.name,
      offers: offers,
      cheapest: offers[0] || null,
      best_value: offers[0] || null // يمكنك إضافة معادلة أفضل قيمة هنا لاحقاً
    };
  });

  return NextResponse.json(results);
}
