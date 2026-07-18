// Starter 首页：init 后立即可 build 出一个像样的占位首屏。
// 建站时应整体替换本页内容为用户要求的真实站点，不保留占位文案。
import { Card, Col, Row } from "antd";

export default function Home() {
  return (
    <div className="mx-auto max-w-5xl px-6 py-12">
      <header className="mb-10 text-center">
        <h1 className="text-3xl font-bold text-gray-900">站点正在搭建中</h1>
        <p className="mt-3 text-gray-500">内容即将上线，敬请期待</p>
      </header>
      <Row gutter={[16, 16]}>
        {["板块一", "板块二", "板块三"].map((t) => (
          <Col xs={24} md={8} key={t}>
            <Card title={t}>
              <div className="h-16 rounded bg-gray-100" />
            </Card>
          </Col>
        ))}
      </Row>
    </div>
  );
}
