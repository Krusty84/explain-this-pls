import React from 'react';
interface Props { value: string }
export const Card = ({value}: Props) => <main>{value}</main>;
function App() {
  return <Card value="Hi" />;
}
