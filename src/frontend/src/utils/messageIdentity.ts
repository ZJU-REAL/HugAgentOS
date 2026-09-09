/** 本地新建消息的身份。
 *
 *  后端主键 message_id 只有落库之后才拿得到，在那之前这条消息同样要有一个不会
 *  重复的身份——否则渲染只能退回时间戳，而同一轮的提问与回答是后端在同一时刻
 *  建的，时间戳必然撞车。 */
let counter = 0;

export function newMessageUid(): string {
  counter += 1;
  return `local_${Date.now().toString(36)}_${counter.toString(36)}_${Math.random().toString(36).slice(2, 6)}`;
}
