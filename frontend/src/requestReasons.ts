import type { Plan, WorkRequest } from './types';
import { time } from './format';

export const reasonNames: Record<string, string> = {
  off_shift: 'Не выходит на эту смену',
  outside_event: 'Для этой заявки нужно разрешить перестановки',
  fixed_engineer: 'За заявкой закреплён другой инженер',
  fixed_order: 'Нужно изменить порядок уже назначенных работ',
  schedule_change: 'Потребуется сильнее сдвинуть время других клиентов',
  unavailable: 'Недоступен',
  skill: 'Нет нужной квалификации',
  transport: 'Не подходит вид транспорта',
  equipment_routers: 'Выданных роутеров не хватит на эту заявку и остальные заказы',
  equipment_set_top_boxes: 'Выданных ТВ-приставок не хватит на все заказы',
  window: 'Не успевает приехать в указанное время',
  shift: 'Не успевает закончить работу до конца смены',
  unreachable: 'Не удалось построить путь до адреса',
  zone: 'Работает в другой зоне',
  not_selected: 'Не удалось включить заявку в найденное расписание',
  search_budget: 'Можно добавить заявку, но расчёт закончился раньше',
  no_feasible_append: 'В конце маршрута для этой заявки нет подходящего времени',
  no_feasible_insertion: 'Не удаётся совместить с другими выездами',
};

export function explainRejection(
  rejection: Plan['unassigned'][number],
  job: WorkRequest,
  missingCoordinates: boolean,
) {
  const checks = Object.values(rejection.checks);
  const available = checks.filter((reason) => !['off_shift', 'unavailable'].includes(reason));
  if (missingCoordinates)
    return {
      title: 'Нужно уточнить адрес',
      action: 'Запустите расчёт во вкладке «План» — появится окно уточнения адресов.',
    };
  if (rejection.reason === 'no_engineers')
    return {
      title: 'Нет доступных инженеров',
      action: 'Проверьте состав инженеров в параметрах планирования.',
    };
  if (checks.length && checks.every((reason) => reason === 'off_shift'))
    return {
      title: 'В смене нет инженеров',
      action: 'Рассчитайте новый план с доступным составом инженеров.',
    };
  if (checks.length && !available.length)
    return {
      title: 'Все инженеры смены недоступны',
      action: 'Проверьте доступность инженеров. Заявка пока остаётся без исполнителя.',
    };
  if (available.length && available.every((reason) => reason === 'skill'))
    return {
      title: 'Нет инженера с нужной квалификацией',
      action: 'Проверьте навыки доступных инженеров в параметрах планирования.',
    };
  if (available.length && available.every((reason) => ['skill', 'transport'].includes(reason)))
    return {
      title: 'Не найден инженер с нужным навыком и транспортом',
      action: 'Проверьте требования заявки и параметры доступных инженеров.',
    };
  if (checks.includes('search_budget'))
    return {
      title: 'Расчёт закончился до назначения этой заявки',
      action: 'Попробуйте рассчитать план заново: для заявки найден подходящий вариант.',
    };
  const suitable = available.filter((reason) => !['skill', 'transport', 'zone'].includes(reason));
  if (suitable.length && suitable.every((reason) => reason.startsWith('equipment_')))
    return {
      title: 'Не хватает выданного оборудования',
      action: 'У подходящих инженеров устройства уже нужны для других заказов.',
    };
  if (suitable.length && suitable.every((reason) => reason === 'unreachable'))
    return {
      title: 'Нет маршрута до клиента',
      action: 'Проверьте точку адреса и доступный транспорт инженеров.',
    };
  if (suitable.length && suitable.every((reason) => reason === 'window'))
    return {
      title: `Не получается приехать с ${time(job.window_start_s)} до ${time(job.window_end_s)}`,
      action:
        'Добавление заявки нарушает время этого или других клиентов. Попробуйте переставить выезды или согласовать другое время.',
    };
  if (suitable.length && suitable.every((reason) => reason === 'shift'))
    return {
      title: 'Работа не помещается в смену',
      action: `На месте нужно ${Math.ceil(job.service_s / 60)} мин, плюс дорога. Подходящие инженеры не успевают закончить до конца смены.`,
    };
  if (
    suitable.some((reason) =>
      ['fixed_order', 'fixed_engineer', 'schedule_change', 'outside_event'].includes(reason),
    )
  )
    return {
      title: 'Мешают ранее согласованные выезды',
      action: 'Попробуйте режим «Разрешить перестановки» и проверьте изменения перед принятием.',
    };
  return {
    title: 'В текущем расписании не нашлось подходящего времени',
    action:
      'Нужна перестановка других выездов или изменение доступного состава. Повторный расчёт может найти другой вариант.',
  };
}
