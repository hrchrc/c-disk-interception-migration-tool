// 嵌入 Windows 版本资源(build.rs):编译 resources/app.rc → 链接进 exe。
// 目的:补全 PE 版本信息(公司/产品/描述),消除"无信息未知程序"特征,
// 降低 360 等启发式杀软的误报率(正规软件元信息完备是合规做法)。
fn main() {
    embed_resource::compile("resources/app.rc", embed_resource::NONE);
}
