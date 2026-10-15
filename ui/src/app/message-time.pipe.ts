import { Pipe, type PipeTransform } from '@angular/core';

function pad(value: number, width = 2): string {
  return String(value).padStart(width, '0');
}

@Pipe({
  name: 'messageTime',
  standalone: true,
})
export class MessageTimePipe implements PipeTransform {
  transform(value: string | null | undefined): string {
    if (!value) return '';

    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '';

    // Local getters use the browser's timezone, including the date's daylight-saving offset.
    const calendar = [
      pad(date.getFullYear(), 4),
      pad(date.getMonth() + 1),
      pad(date.getDate()),
    ].join('-');
    const clock = [pad(date.getHours()), pad(date.getMinutes()), pad(date.getSeconds())].join(':');
    return `${calendar} ${clock}`;
  }
}
